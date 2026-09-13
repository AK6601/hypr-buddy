//! Speech Bubble Renderer
//!
//! Renders a speech bubble above the buddy with:
//! - Rounded rectangle with a tail pointing downward to the character
//! - Typewriter text reveal effect
//! - Auto-dismiss after the text is fully revealed + configurable delay
//! - Word wrapping at a configurable max width
//!
//! The speech bubble is rendered to an RGBA pixel buffer that gets composited
//! on top of the sprite by the main renderer.

use fontdue::{Font, FontSettings};
use std::{sync::OnceLock, time::Duration};

fn font() -> &'static Font {
    static FONT: OnceLock<Font> = OnceLock::new();
    FONT.get_or_init(|| {
        Font::from_bytes(
            include_bytes!("../../assets/fonts/DejaVuSansMono.ttf") as &[u8],
            FontSettings::default(),
        )
        .expect("bundled font")
    })
}

/// A speech bubble currently being displayed.
pub struct SpeechBubble {
    /// The full text to display.
    full_text: String,
    /// Total character count (chars, not bytes).
    total_chars: usize,
    /// Number of characters currently revealed.
    revealed_chars: usize,
    /// Characters per second for the typewriter effect.
    chars_per_second: f32,
    /// Accumulated time for character reveal.
    char_timer: f32,
    /// Seconds to wait after full reveal before dismissing.
    dismiss_delay: f64,
    /// Timer counting down after full reveal.
    dismiss_timer: f64,
    /// Whether the bubble has been dismissed.
    dismissed: bool,
    /// Max width in pixels.
    max_width: u32,
    /// Font size.
    font_size: u32,
}

impl SpeechBubble {
    pub fn new(
        text: String,
        typewriter_speed: u32,
        dismiss_delay: f64,
        max_width: u32,
        font_size: u32,
    ) -> Self {
        let total_chars = text.chars().count();
        Self {
            full_text: text,
            total_chars,
            revealed_chars: 0,
            chars_per_second: typewriter_speed.max(1) as f32,
            char_timer: 0.0,
            dismiss_delay,
            dismiss_timer: 0.0,
            dismissed: false,
            max_width,
            font_size,
        }
    }

    /// Advance the speech bubble state. Returns true if visual state changed.
    pub fn tick(&mut self, dt: Duration) -> bool {
        if self.dismissed {
            return false;
        }

        let dt_secs = dt.as_secs_f32();

        if self.revealed_chars < self.total_chars {
            // Still revealing characters
            self.char_timer += dt_secs;
            let chars_to_reveal = (self.char_timer * self.chars_per_second) as usize;
            if chars_to_reveal > 0 {
                self.char_timer -= chars_to_reveal as f32 / self.chars_per_second;
                let old = self.revealed_chars;
                self.revealed_chars = (self.revealed_chars + chars_to_reveal).min(self.total_chars);
                return self.revealed_chars != old;
            }
            false
        } else {
            // Fully revealed — count down dismiss timer
            self.dismiss_timer += dt_secs as f64;
            if self.dismiss_timer >= self.dismiss_delay {
                self.dismissed = true;
                return true;
            }
            false
        }
    }

    pub fn is_dismissed(&self) -> bool {
        self.dismissed
    }

    /// Render the speech bubble to RGBA pixel data.
    ///
    /// Returns `(pixels, dx, dy, width, height)` where `dx`/`dy` are the
    /// bubble's offset *relative to the sprite's top-left corner*. The caller
    /// adds the sprite's current on-screen (local) position so the bubble
    /// tracks the sprite wherever it is, rather than sitting in a fixed corner
    /// of the buffer.
    ///
    /// `sprite_width` is the on-screen sprite box width (used to right-align
    /// the bubble over the sprite). The bubble is placed just above the sprite
    /// (negative `dy`, so it floats over the character's head).
    pub fn render(&self, sprite_width: u32) -> (Vec<u8>, i32, i32, u32, u32) {
        let visible_text: String = self.full_text.chars().take(self.revealed_chars).collect();

        // Simple text layout calculation
        let char_width = font()
            .metrics('M', self.font_size as f32)
            .advance_width
            .ceil() as u32;
        let line_height = (self.font_size as f32 * 1.4) as u32;
        let padding: u32 = 12;
        let tail_height: u32 = 10;

        // Word wrap
        let max_chars_per_line =
            (self.max_width.saturating_sub(padding * 2) / char_width.max(1)).max(1) as usize;
        let visible_lines = word_wrap(&visible_text, max_chars_per_line);
        let lines = &visible_lines[visible_lines.len().saturating_sub(8)..];
        let full_lines = word_wrap(&self.full_text, max_chars_per_line);

        // Calculate bubble dimensions
        let text_width = full_lines
            .iter()
            .map(|l| l.chars().count() as u32 * char_width)
            .max()
            .unwrap_or(char_width * 3);
        let bubble_width = (text_width + padding * 2)
            .min(self.max_width)
            .max(padding * 4);
        let bubble_height =
            full_lines.len().min(8) as u32 * line_height + padding * 2 + tail_height;

        let total_width = bubble_width;
        let total_height = bubble_height;

        let mut pixels = vec![0u8; (total_width * total_height * 4) as usize];

        // Draw rounded rectangle background
        let corner_radius = 8u32;
        let bg_color: [u8; 4] = [25, 28, 42, 245]; // White, slightly transparent
        let border_color: [u8; 4] = [110, 211, 211, 255];

        for y in 0..bubble_height - tail_height {
            for x in 0..bubble_width {
                let idx = ((y * total_width + x) * 4) as usize;

                // Check if pixel is inside the rounded rectangle
                let inside = is_inside_rounded_rect(
                    x,
                    y,
                    bubble_width,
                    bubble_height - tail_height,
                    corner_radius,
                );
                let on_border = !inside
                    && is_inside_rounded_rect(
                        x,
                        y,
                        bubble_width,
                        bubble_height - tail_height,
                        corner_radius + 1,
                    );

                if inside {
                    pixels[idx..idx + 4].copy_from_slice(&bg_color);
                } else if on_border {
                    pixels[idx..idx + 4].copy_from_slice(&border_color);
                }
            }
        }

        // Draw tail (triangle pointing down-right toward the buddy)
        let tail_x = bubble_width * 3 / 4;
        let tail_top = bubble_height - tail_height - 1;
        for dy in 0..tail_height {
            let half_width = (tail_height - dy) as i32;
            for dx in -half_width..=half_width {
                let x = (tail_x as i32 + dx) as u32;
                let y = tail_top + dy;
                if x < total_width && y < total_height {
                    let idx = ((y * total_width + x) * 4) as usize;
                    if dx.unsigned_abs() == half_width as u32 {
                        pixels[idx..idx + 4].copy_from_slice(&border_color);
                    } else {
                        pixels[idx..idx + 4].copy_from_slice(&bg_color);
                    }
                }
            }
        }

        let text_color = [232u8, 236, 247];
        for (line_idx, line) in lines.iter().enumerate() {
            let baseline =
                padding as i32 + line_idx as i32 * line_height as i32 + self.font_size as i32;
            for (ch_idx, ch) in line.chars().enumerate() {
                let (metrics, coverage) = font().rasterize(ch, self.font_size as f32);
                let x0 = padding as i32 + ch_idx as i32 * char_width as i32 + metrics.xmin;
                let y0 = baseline - metrics.height as i32 - metrics.ymin;
                for y in 0..metrics.height {
                    for x in 0..metrics.width {
                        let px = x0 + x as i32;
                        let py = y0 + y as i32;
                        if px < 0 || py < 0 || px >= total_width as i32 || py >= total_height as i32
                        {
                            continue;
                        }
                        let a = coverage[y * metrics.width + x] as f32 / 255.;
                        let i = ((py as u32 * total_width + px as u32) * 4) as usize;
                        for c in 0..3 {
                            pixels[i + c] =
                                (pixels[i + c] as f32 * (1. - a) + text_color[c] as f32 * a) as u8;
                        }
                        pixels[i + 3] = (pixels[i + 3] as f32 * (1. - a) + 255. * a) as u8;
                    }
                }
            }
        }

        // Position relative to the sprite's top-left corner:
        // - right-aligned over the sprite (so the tail points down at the head)
        // - lifted up by its own height so it sits ABOVE the sprite box
        let dx = sprite_width as i32 - bubble_width as i32;
        let dy = -(total_height as i32);

        (pixels, dx, dy, total_width, total_height)
    }
}

/// Check if a point is inside a rounded rectangle.
fn is_inside_rounded_rect(x: u32, y: u32, w: u32, h: u32, r: u32) -> bool {
    if x >= w || y >= h {
        return false;
    }

    // Check corners
    let corners = [
        (r, r),                                     // top-left
        (w.saturating_sub(r), r),                   // top-right
        (r, h.saturating_sub(r)),                   // bottom-left
        (w.saturating_sub(r), h.saturating_sub(r)), // bottom-right
    ];

    // If in a corner region, check distance from corner center
    let in_corner = (x < r || x >= w - r) && (y < r || y >= h - r);
    if in_corner {
        for &(cx, cy) in &corners {
            let dx = x as f32 - cx as f32;
            let dy = y as f32 - cy as f32;
            if dx * dx + dy * dy <= (r as f32) * (r as f32) {
                return true;
            }
        }
        return false;
    }

    true
}

/// Simple word wrapping.
fn word_wrap(text: &str, max_chars: usize) -> Vec<String> {
    if max_chars == 0 {
        return vec![text.to_string()];
    }

    let mut lines = Vec::new();
    for paragraph in text.split('\n') {
        let mut line = String::new();
        for word in paragraph.split_whitespace() {
            if !line.is_empty() && line.chars().count() + 1 + word.chars().count() > max_chars {
                lines.push(std::mem::take(&mut line));
            }
            if !line.is_empty() {
                line.push(' ');
            }
            for ch in word.chars() {
                if line.chars().count() >= max_chars {
                    lines.push(std::mem::take(&mut line));
                }
                line.push(ch);
            }
        }
        lines.push(line);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn wraps_unicode_and_long_words() {
        assert_eq!(word_wrap("ééééé", 2), vec!["éé", "éé", "é"]);
        assert_eq!(word_wrap("a\nb", 20), vec!["a", "b"]);
    }
    #[test]
    fn bubble_size_stays_stable_while_revealing() {
        let mut b = SpeechBubble::new(
            "A readable sentence with several words.".into(),
            30,
            3.,
            200,
            14,
        );
        let first = b.render(224);
        b.tick(Duration::from_secs(1));
        let second = b.render(224);
        assert_eq!((first.3, first.4), (second.3, second.4));
        assert_ne!(first.0, second.0);
    }
}
