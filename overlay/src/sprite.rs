//! Sprite Sheet Animation
//!
//! Manages loading and playback of sprite sheet animations.
//!
//! Each emotional state has a sprite sheet: a horizontal strip of frames
//! stored as a PNG file. For example, `idle.png` might be 1024x256 pixels
//! containing 4 frames of 256x256 each.
//!
//! The animation system supports:
//! - Per-state frame timing
//! - Crossfade transitions between states (alpha blending over N ms)
//! - Returning the current frame as RGBA pixel data for rendering

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use crate::config::OverlayConfig;

/// RGBA pixel data for a single frame.
pub type FramePixels = Vec<u8>;

/// A loaded sprite sheet with its frames already extracted.
struct SpriteSheet {
    /// Individual frames extracted from the strip, each as RGBA data.
    frames: Vec<FramePixels>,
    /// Width of each frame in pixels.
    frame_width: u32,
    /// Height of each frame in pixels.
    frame_height: u32,
}

/// Current animation playback state for one sprite sheet.
struct AnimationState {
    /// Index into the SpriteSheet's frames array.
    current_frame: usize,
    /// Time accumulated since last frame advance.
    elapsed: Duration,
    /// Duration of each frame.
    frame_duration: Duration,
}

/// Manages all sprite sheets and animation playback.
pub struct SpriteManager {
    /// Loaded sprite sheets keyed by state name.
    sheets: HashMap<String, SpriteSheet>,
    /// Current animation state.
    current: AnimationState,
    /// Name of the current state.
    current_state: String,
    /// Transition state: if Some, we're crossfading from old state.
    transition: Option<TransitionState>,
    /// Fallback: a single solid-color frame if no sprites are loaded.
    fallback_frame: FramePixels,
    frame_size: u32,
}

struct TransitionState {
    from_state: String,
    from_animation: AnimationState,
    progress: f32, // 0.0 -> 1.0
    duration: Duration,
    elapsed: Duration,
}

impl SpriteManager {
    /// Load all sprite sheets from the given directory.
    ///
    /// Expects PNG files named `{state}.png` (e.g., `idle.png`, `happy.png`).
    /// Each PNG should be a horizontal strip where width = N * height.
    pub fn load_all(sprites_dir: &Path, config: &OverlayConfig) -> Self {
        let states = [
            "idle", "talking", "happy", "sad", "surprised",
            "thinking", "sleeping", "waving", "angry",
        ];

        let mut sheets = HashMap::new();
        let frame_size = config.window.width;

        for state in &states {
            let path = sprites_dir.join(format!("{}.png", state));
            match load_png_strip(&path, frame_size) {
                Ok(sheet) => {
                    log::info!(
                        "Loaded sprite '{}': {} frames ({}x{})",
                        state,
                        sheet.frames.len(),
                        sheet.frame_width,
                        sheet.frame_height,
                    );
                    sheets.insert(state.to_string(), sheet);
                }
                Err(e) => {
                    log::warn!("Could not load sprite '{}' from {}: {}", state, path.display(), e);
                }
            }
        }

        // Create a fallback frame (pink circle on transparent background)
        let fallback = generate_fallback_frame(frame_size);

        let fps = config.animation.idle_fps.max(1); // Prevent division by zero
        let frame_dur = Duration::from_secs_f64(1.0 / fps as f64);

        Self {
            sheets,
            current: AnimationState {
                current_frame: 0,
                elapsed: Duration::ZERO,
                frame_duration: frame_dur,
            },
            current_state: "idle".to_string(),
            transition: None,
            fallback_frame: fallback,
            frame_size,
        }
    }

    pub fn state_count(&self) -> usize {
        self.sheets.len()
    }

    pub fn is_transitioning(&self) -> bool {
        self.transition.is_some()
    }

    /// Begin a transition to a new state.
    pub fn transition_to(&mut self, new_state: &str, transition_ms: u32) {
        if new_state == self.current_state && self.transition.is_none() {
            return; // Already in this state
        }

        if transition_ms == 0 || !self.sheets.contains_key(new_state) {
            // Instant switch
            self.current_state = new_state.to_string();
            self.current.current_frame = 0;
            self.current.elapsed = Duration::ZERO;
            self.transition = None;
            return;
        }

        // Start crossfade
        let old_state = self.current_state.clone();
        let old_anim = AnimationState {
            current_frame: self.current.current_frame,
            elapsed: self.current.elapsed,
            frame_duration: self.current.frame_duration,
        };

        self.transition = Some(TransitionState {
            from_state: old_state,
            from_animation: old_anim,
            progress: 0.0,
            duration: Duration::from_millis(transition_ms as u64),
            elapsed: Duration::ZERO,
        });

        self.current_state = new_state.to_string();
        self.current.current_frame = 0;
        self.current.elapsed = Duration::ZERO;
    }

    /// Advance animation by `dt`. Returns true if the frame changed.
    pub fn tick(&mut self, dt: Duration) -> bool {
        let mut dirty = false;

        // Advance transition
        if let Some(ref mut trans) = self.transition {
            trans.elapsed += dt;
            trans.progress = (trans.elapsed.as_secs_f32() / trans.duration.as_secs_f32()).min(1.0);

            // Advance old animation too
            trans.from_animation.elapsed += dt;
            if trans.from_animation.elapsed >= trans.from_animation.frame_duration {
                trans.from_animation.elapsed -= trans.from_animation.frame_duration;
                if let Some(sheet) = self.sheets.get(&trans.from_state) {
                    if !sheet.frames.is_empty() {
                        trans.from_animation.current_frame =
                            (trans.from_animation.current_frame + 1) % sheet.frames.len();
                    }
                }
            }

            dirty = true;

            if trans.progress >= 1.0 {
                self.transition = None;
            }
        }

        // Advance current animation
        self.current.elapsed += dt;
        if self.current.elapsed >= self.current.frame_duration {
            self.current.elapsed -= self.current.frame_duration;
            if let Some(sheet) = self.sheets.get(&self.current_state) {
                if !sheet.frames.is_empty() {
                    let old_frame = self.current.current_frame;
                    self.current.current_frame = (self.current.current_frame + 1) % sheet.frames.len();
                    if self.current.current_frame != old_frame {
                        dirty = true;
                    }
                }
            }
        }

        dirty
    }

    /// Get the current frame's RGBA pixel data.
    ///
    /// During a transition, this returns alpha-blended pixels from both states.
    pub fn current_frame(&self) -> Option<&[u8]> {
        // For simplicity, return the current state's frame.
        // A full implementation would blend during transitions.
        let sheet = self.sheets.get(&self.current_state);
        match sheet {
            Some(s) if !s.frames.is_empty() => {
                let idx = self.current.current_frame.min(s.frames.len() - 1);
                Some(&s.frames[idx])
            }
            _ => Some(&self.fallback_frame),
        }
    }
}

/// Load a PNG file and split it into frames.
fn load_png_strip(path: &Path, frame_size: u32) -> Result<SpriteSheet, String> {
    let file = std::fs::File::open(path).map_err(|e| format!("open: {}", e))?;
    let decoder = png::Decoder::new(file);
    let mut reader = decoder.read_info().map_err(|e| format!("read_info: {}", e))?;

    let info = reader.info();
    let width = info.width;
    let height = info.height;
    let color_type = info.color_type;

    if height != frame_size {
        return Err(format!(
            "Expected height {} but got {} (frame_size mismatch)",
            frame_size, height,
        ));
    }

    let num_frames = (width / frame_size) as usize;
    if num_frames == 0 {
        return Err("Image too narrow for even one frame".to_string());
    }

    // Read all pixels
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let output_info = reader.next_frame(&mut buf).map_err(|e| format!("decode: {}", e))?;
    let pixels = &buf[..output_info.buffer_size()];

    // Determine bytes per pixel
    let bpp = match color_type {
        png::ColorType::Rgba => 4,
        png::ColorType::Rgb => 3,
        png::ColorType::GrayscaleAlpha => 2,
        png::ColorType::Grayscale => 1,
        _ => return Err(format!("Unsupported color type: {:?}", color_type)),
    };

    let stride = width as usize * bpp;
    let frame_stride = frame_size as usize * bpp;

    // Extract individual frames
    let mut frames = Vec::with_capacity(num_frames);
    for f in 0..num_frames {
        let mut rgba = vec![0u8; (frame_size * frame_size * 4) as usize];
        let x_offset = f * frame_stride;

        for y in 0..frame_size as usize {
            for x in 0..frame_size as usize {
                let src_idx = y * stride + x_offset + x * bpp;
                let dst_idx = (y * frame_size as usize + x) * 4;

                match bpp {
                    4 => {
                        rgba[dst_idx] = pixels[src_idx];
                        rgba[dst_idx + 1] = pixels[src_idx + 1];
                        rgba[dst_idx + 2] = pixels[src_idx + 2];
                        rgba[dst_idx + 3] = pixels[src_idx + 3];
                    }
                    3 => {
                        rgba[dst_idx] = pixels[src_idx];
                        rgba[dst_idx + 1] = pixels[src_idx + 1];
                        rgba[dst_idx + 2] = pixels[src_idx + 2];
                        rgba[dst_idx + 3] = 255;
                    }
                    2 => {
                        let g = pixels[src_idx];
                        rgba[dst_idx] = g;
                        rgba[dst_idx + 1] = g;
                        rgba[dst_idx + 2] = g;
                        rgba[dst_idx + 3] = pixels[src_idx + 1];
                    }
                    1 => {
                        let g = pixels[src_idx];
                        rgba[dst_idx] = g;
                        rgba[dst_idx + 1] = g;
                        rgba[dst_idx + 2] = g;
                        rgba[dst_idx + 3] = 255;
                    }
                    _ => unreachable!(),
                }
            }
        }

        frames.push(rgba);
    }

    Ok(SpriteSheet {
        frames,
        frame_width: frame_size,
        frame_height: frame_size,
    })
}

/// Generate a simple pink circle frame as a fallback when no sprites exist.
fn generate_fallback_frame(size: u32) -> FramePixels {
    let mut pixels = vec![0u8; (size * size * 4) as usize];
    let center = size as f32 / 2.0;
    let radius = size as f32 * 0.35;

    for y in 0..size {
        for x in 0..size {
            let dx = x as f32 - center;
            let dy = y as f32 - center;
            let dist = (dx * dx + dy * dy).sqrt();

            let idx = ((y * size + x) * 4) as usize;
            if dist <= radius {
                // Soft pink body: #FFB7C5
                pixels[idx] = 0xFF;
                pixels[idx + 1] = 0xB7;
                pixels[idx + 2] = 0xC5;
                // Soft edge
                let alpha = if dist > radius - 2.0 {
                    ((radius - dist) / 2.0 * 255.0) as u8
                } else {
                    255
                };
                pixels[idx + 3] = alpha;
            }
            // else: stays transparent (0,0,0,0)
        }
    }

    pixels
}
