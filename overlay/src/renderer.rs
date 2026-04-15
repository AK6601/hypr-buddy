//! Shared Memory Renderer
//!
//! Manages the pixel buffer that gets displayed on screen.
//!
//! # Wayland Rendering Pipeline
//!
//! On Wayland, rendering works through shared memory (wl_shm):
//!
//! 1. Create a temporary file and mmap it (this is our pixel buffer)
//! 2. Create a `wl_shm_pool` from that file descriptor
//! 3. Create a `wl_buffer` from the pool (specifying format, stride, size)
//! 4. Write pixel data to the mmap'd memory
//! 5. Attach the buffer to our `wl_surface` and commit
//! 6. The compositor reads our pixels and displays them
//!
//! This module handles step 1 and 4 — the actual buffer management.
//! The Wayland surface management would be done via smithay-client-toolkit
//! when running under an actual Wayland compositor.
//!
//! For development/testing without a compositor, we just maintain the
//! pixel buffer in memory and log frame events.

/// Software renderer that manages an RGBA pixel buffer.
///
/// In production, this buffer would be backed by a wl_shm mmap'd file.
/// For now, it's a simple Vec<u8> that we write to.
pub struct ShmRenderer {
    /// RGBA pixel data (width * height * 4 bytes).
    buffer: Vec<u8>,
    width: u32,
    height: u32,
    /// Frame counter for debug logging.
    frame_count: u64,
}

impl ShmRenderer {
    /// Create a new renderer with the given dimensions.
    ///
    /// The buffer is pre-multiplied ARGB format (what Wayland expects),
    /// but we work in RGBA internally and convert on present().
    pub fn new(width: u32, height: u32) -> Self {
        let size = (width * height * 4) as usize;
        Self {
            buffer: vec![0u8; size],
            width,
            height,
            frame_count: 0,
        }
    }

    /// Clear the buffer to fully transparent.
    pub fn clear(&mut self) {
        self.buffer.fill(0);
    }

    /// Blit sprite pixel data (RGBA) centered in the buffer.
    ///
    /// The sprite is expected to have matching dimensions.
    /// Pixels with alpha > 0 are copied; transparent pixels are skipped.
    pub fn blit_sprite(&mut self, sprite_rgba: &[u8], sprite_w: u32, sprite_h: u32) {
        let expected = (sprite_w * sprite_h * 4) as usize;
        if sprite_rgba.len() < expected {
            return;
        }

        // Center the sprite if it's smaller than the buffer
        let x_offset = (self.width.saturating_sub(sprite_w)) / 2;
        let y_offset = (self.height.saturating_sub(sprite_h)) / 2;

        for y in 0..sprite_h.min(self.height) {
            for x in 0..sprite_w.min(self.width) {
                let src_idx = ((y * sprite_w + x) * 4) as usize;
                let alpha = sprite_rgba[src_idx + 3];
                if alpha == 0 {
                    continue;
                }

                let dst_x = x + x_offset;
                let dst_y = y + y_offset;
                if dst_x >= self.width || dst_y >= self.height {
                    continue;
                }

                let dst_idx = ((dst_y * self.width + dst_x) * 4) as usize;

                if alpha == 255 {
                    // Opaque: straight copy
                    self.buffer[dst_idx..dst_idx + 4]
                        .copy_from_slice(&sprite_rgba[src_idx..src_idx + 4]);
                } else {
                    // Alpha blend: src over dst
                    let sa = alpha as f32 / 255.0;
                    let da = self.buffer[dst_idx + 3] as f32 / 255.0;
                    let out_a = sa + da * (1.0 - sa);

                    if out_a > 0.0 {
                        for c in 0..3 {
                            let sc = sprite_rgba[src_idx + c] as f32;
                            let dc = self.buffer[dst_idx + c] as f32;
                            self.buffer[dst_idx + c] =
                                ((sc * sa + dc * da * (1.0 - sa)) / out_a) as u8;
                        }
                        self.buffer[dst_idx + 3] = (out_a * 255.0) as u8;
                    }
                }
            }
        }
    }

    /// Blit an overlay (like a speech bubble) at the given position.
    pub fn blit_overlay(
        &mut self,
        pixels: &[u8],
        x_off: u32,
        y_off: u32,
        w: u32,
        h: u32,
    ) {
        let expected = (w * h * 4) as usize;
        if pixels.len() < expected {
            return;
        }

        for y in 0..h {
            for x in 0..w {
                let src_idx = ((y * w + x) * 4) as usize;
                let alpha = pixels[src_idx + 3];
                if alpha == 0 {
                    continue;
                }

                let dst_x = x + x_off;
                let dst_y = y + y_off;
                if dst_x >= self.width || dst_y >= self.height {
                    continue;
                }

                let dst_idx = ((dst_y * self.width + dst_x) * 4) as usize;

                if alpha == 255 {
                    self.buffer[dst_idx..dst_idx + 4]
                        .copy_from_slice(&pixels[src_idx..src_idx + 4]);
                } else {
                    // Porter-Duff "over" compositing: src over dst
                    let sa = alpha as f32 / 255.0;
                    let da = self.buffer[dst_idx + 3] as f32 / 255.0;
                    let out_a = sa + da * (1.0 - sa);
                    if out_a > 0.0 {
                        for c in 0..3 {
                            let sc = pixels[src_idx + c] as f32;
                            let dc = self.buffer[dst_idx + c] as f32;
                            self.buffer[dst_idx + c] =
                                ((sc * sa + dc * da * (1.0 - sa)) / out_a) as u8;
                        }
                        self.buffer[dst_idx + 3] = (out_a * 255.0) as u8;
                    }
                }
            }
        }
    }

    /// Increment frame counter (for debug logging).
    pub fn present(&mut self) {
        self.frame_count += 1;
    }

    /// Get a reference to the raw pixel buffer (RGBA).
    #[allow(dead_code)]
    pub fn pixels(&self) -> &[u8] {
        &self.buffer
    }

    /// Get the buffer dimensions.
    #[allow(dead_code)]
    pub fn dimensions(&self) -> (u32, u32) {
        (self.width, self.height)
    }
}
