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

use std::time::Duration;

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
            chars_per_second: typewriter_speed as f32,
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
                self.revealed_chars = (self.revealed_chars + chars_to_reveal)
                    .min(self.total_chars);
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
    /// Returns (pixels, x_offset, y_offset, width, height) for compositing.
    pub fn render(&self, sprite_width: u32) -> (Vec<u8>, u32, u32, u32, u32) {
        let visible_text: String = self.full_text.chars().take(self.revealed_chars).collect();

        // Simple text layout calculation
        let char_width = (self.font_size as f32 * 0.6) as u32;
        let line_height = (self.font_size as f32 * 1.4) as u32;
        let padding: u32 = 12;
        let tail_height: u32 = 10;

        // Word wrap
        let max_chars_per_line = ((self.max_width - padding * 2) / char_width.max(1)) as usize;
        let lines = word_wrap(&visible_text, max_chars_per_line);

        // Calculate bubble dimensions
        let text_width = lines.iter()
            .map(|l| l.len() as u32 * char_width)
            .max()
            .unwrap_or(char_width * 3);
        let bubble_width = (text_width + padding * 2).min(self.max_width).max(padding * 4);
        let bubble_height = lines.len() as u32 * line_height + padding * 2 + tail_height;

        let total_width = bubble_width;
        let total_height = bubble_height;

        let mut pixels = vec![0u8; (total_width * total_height * 4) as usize];

        // Draw rounded rectangle background
        let corner_radius = 8u32;
        let bg_color: [u8; 4] = [255, 255, 255, 230]; // White, slightly transparent
        let border_color: [u8; 4] = [180, 180, 180, 255];

        for y in 0..bubble_height - tail_height {
            for x in 0..bubble_width {
                let idx = ((y * total_width + x) * 4) as usize;

                // Check if pixel is inside the rounded rectangle
                let inside = is_inside_rounded_rect(
                    x, y, bubble_width, bubble_height - tail_height, corner_radius,
                );
                let on_border = !inside && is_inside_rounded_rect(
                    x, y, bubble_width, bubble_height - tail_height, corner_radius + 1,
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

        // Draw text (simple rasterization — each char as a filled rectangle)
        // In a real implementation, we'd use fontdue for proper glyph rendering.
        let text_color: [u8; 4] = [50, 50, 50, 255];
        for (line_idx, line) in lines.iter().enumerate() {
            let y_start = padding + line_idx as u32 * line_height;
            for (ch_idx, ch) in line.chars().enumerate() {
                let x_start = padding + ch_idx as u32 * char_width;
                // Draw a simple representation of each character
                // (In production, fontdue would render actual glyphs here)
                let glyph_w = (char_width as f32 * 0.7) as u32;
                let glyph_h = (self.font_size as f32 * 0.8) as u32;
                let glyph_y = y_start + (line_height - glyph_h) / 2;

                if ch == ' ' {
                    continue;
                }

                for gy in glyph_y..glyph_y + glyph_h {
                    for gx in x_start..x_start + glyph_w {
                        if gx < total_width && gy < total_height {
                            let idx = ((gy * total_width + gx) * 4) as usize;
                            // Simple: draw a small filled block per character
                            // This gives a "pixel font" appearance for placeholders
                            pixels[idx..idx + 4].copy_from_slice(&text_color);
                        }
                    }
                }
            }
        }

        // Position: above the sprite, right-aligned
        let x_offset = sprite_width.saturating_sub(bubble_width);
        let y_offset = 0; // Above the sprite

        (pixels, x_offset, y_offset, total_width, total_height)
    }
}

/// Check if a point is inside a rounded rectangle.
fn is_inside_rounded_rect(x: u32, y: u32, w: u32, h: u32, r: u32) -> bool {
    if x >= w || y >= h {
        return false;
    }

    // Check corners
    let corners = [
        (r, r),                     // top-left
        (w.saturating_sub(r), r),   // top-right
        (r, h.saturating_sub(r)),   // bottom-left
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
    let mut current_line = String::new();

    for word in text.split_whitespace() {
        if current_line.is_empty() {
            current_line = word.to_string();
        } else if current_line.len() + 1 + word.len() <= max_chars {
            current_line.push(' ');
            current_line.push_str(word);
        } else {
            lines.push(current_line);
            current_line = word.to_string();
        }
    }

    if !current_line.is_empty() {
        lines.push(current_line);
    }

    if lines.is_empty() {
        lines.push(String::new());
    }

    lines
}
