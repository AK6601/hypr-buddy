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
        }
    }

    /// Resize the renderer's backing buffer in place.
    ///
    /// The buffer is now sized to match the *output* it renders to (full
    /// screen), not the 256x256 sprite. Each output surface owns its own
    /// renderer, so when the compositor tells us an output's pixel size we
    /// resize the matching renderer here. No-op if the size is unchanged.
    pub fn resize(&mut self, width: u32, height: u32) {
        if width == self.width && height == self.height {
            return;
        }
        self.width = width;
        self.height = height;
        self.buffer = vec![0u8; (width as usize) * (height as usize) * 4];
    }

    /// Clear the buffer to fully transparent.
    pub fn clear(&mut self) {
        self.buffer.fill(0);
    }

    /// Blit a single source pixel (RGBA) over the destination buffer at
    /// `(dst_x, dst_y)` using Porter-Duff "src over dst" compositing.
    ///
    /// Inlined so the two public blit helpers below share identical blending.
    #[inline]
    fn blend_pixel(&mut self, dst_x: u32, dst_y: u32, src: &[u8]) {
        // Bounds guard: protects every blit path from out-of-range writes
        // (e.g. a sprite/bubble straddling an edge on an unusually small output).
        if dst_x >= self.width || dst_y >= self.height {
            return;
        }
        let alpha = src[3];
        if alpha == 0 {
            return;
        }
        let dst_idx = ((dst_y * self.width + dst_x) * 4) as usize;

        if alpha == 255 {
            // Opaque: straight copy.
            self.buffer[dst_idx..dst_idx + 4].copy_from_slice(&src[..4]);
            return;
        }

        // Alpha blend: src over dst.
        let sa = alpha as f32 / 255.0;
        let da = self.buffer[dst_idx + 3] as f32 / 255.0;
        let out_a = sa + da * (1.0 - sa);
        if out_a > 0.0 {
            for c in 0..3 {
                let sc = src[c] as f32;
                let dc = self.buffer[dst_idx + c] as f32;
                self.buffer[dst_idx + c] = ((sc * sa + dc * da * (1.0 - sa)) / out_a) as u8;
            }
            self.buffer[dst_idx + 3] = (out_a * 255.0) as u8;
        }
    }

    /// Blit sprite pixel data (RGBA) at an arbitrary signed offset `(x, y)`
    /// within the full-output-sized buffer.
    ///
    /// Unlike the old `blit_sprite`, this does NOT center the sprite — the
    /// caller computes where the sprite's top-left corner should land
    /// (output-local coordinates). Negative offsets are supported so the
    /// sprite can straddle the top/left edge of the output; anything outside
    /// the buffer is clipped. Fully-transparent source pixels are skipped.
    pub fn blit_sprite_at(
        &mut self,
        sprite_rgba: &[u8],
        sprite_w: u32,
        sprite_h: u32,
        x: i32,
        y: i32,
    ) {
        let expected = (sprite_w * sprite_h * 4) as usize;
        if sprite_rgba.len() < expected {
            return;
        }

        // Clip the source rectangle to the buffer bounds. `sx0/sy0` are the
        // first source columns/rows that land inside the buffer.
        let sx0 = if x < 0 { (-x) as u32 } else { 0 };
        let sy0 = if y < 0 { (-y) as u32 } else { 0 };
        if sx0 >= sprite_w || sy0 >= sprite_h {
            return; // entirely off the left/top edge
        }
        // Last source column/row that still lands inside the right/bottom edge.
        let sx1 = if x >= 0 {
            sprite_w.min(self.width.saturating_sub(x as u32))
        } else {
            sprite_w
        };
        let sy1 = if y >= 0 {
            sprite_h.min(self.height.saturating_sub(y as u32))
        } else {
            sprite_h
        };

        for sy in sy0..sy1 {
            let dst_y = (y + sy as i32) as u32;
            for sx in sx0..sx1 {
                let dst_x = (x + sx as i32) as u32;
                let src_idx = ((sy * sprite_w + sx) * 4) as usize;
                self.blend_pixel(dst_x, dst_y, &sprite_rgba[src_idx..src_idx + 4]);
            }
        }
    }

    /// Blit an overlay (like a speech bubble) at the given signed offset.
    ///
    /// Accepts signed `x_off/y_off` so the bubble can be positioned relative
    /// to the sprite's current on-screen location (which may be partly off
    /// the edge). Clipped to the buffer bounds.
    pub fn blit_overlay(&mut self, pixels: &[u8], x_off: i32, y_off: i32, w: u32, h: u32) {
        let expected = (w * h * 4) as usize;
        if pixels.len() < expected {
            return;
        }

        let sx0 = if x_off < 0 { (-x_off) as u32 } else { 0 };
        let sy0 = if y_off < 0 { (-y_off) as u32 } else { 0 };
        if sx0 >= w || sy0 >= h {
            return;
        }
        let sx1 = if x_off >= 0 {
            w.min(self.width.saturating_sub(x_off as u32))
        } else {
            w
        };
        let sy1 = if y_off >= 0 {
            h.min(self.height.saturating_sub(y_off as u32))
        } else {
            h
        };

        for sy in sy0..sy1 {
            let dst_y = (y_off + sy as i32) as u32;
            for sx in sx0..sx1 {
                let dst_x = (x_off + sx as i32) as u32;
                let src_idx = ((sy * w + sx) * 4) as usize;
                self.blend_pixel(dst_x, dst_y, &pixels[src_idx..src_idx + 4]);
            }
        }
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
