//! One registered character, animated continuously. Legacy strips remain loadable.
use crate::config::OverlayConfig;
use std::{collections::HashMap, path::Path, time::Duration};

pub struct SpriteManager {
    sheets: HashMap<String, Vec<Vec<u8>>>,
    base: Option<Vec<u8>>,
    blink: Option<Vec<u8>>,
    pixels: Vec<u8>,
    previous: Vec<u8>,
    width: u32,
    height: u32,
    state: String,
    time: f32,
    state_time: f32,
    transition: f32,
    transition_duration: f32,
    idle_fps: f32,
    talking_fps: f32,
}

impl SpriteManager {
    pub fn load_all(dir: &Path, config: &OverlayConfig) -> Self {
        let w = config.window.width.max(1);
        let h = config.window.height.max(1);
        let character = dir.parent().unwrap_or(dir).join("character");
        let base = if config.animation.procedural {
            load_png(&character.join("shiro.png"))
                .ok()
                .map(|(p, sw, sh)| resize(&p, sw, sh, w, h))
        } else {
            None
        };
        let blink = load_png(&character.join("shiro-blink.png"))
            .ok()
            .map(|(p, sw, sh)| resize(&p, sw, sh, w, h));
        let mut sheets = HashMap::new();
        if base.is_none() {
            for state in [
                "idle",
                "talking",
                "happy",
                "sad",
                "surprised",
                "thinking",
                "sleeping",
                "waving",
                "angry",
                "laughing",
                "blushing",
            ] {
                if let Ok((p, sw, sh)) = load_png(&dir.join(format!("{state}.png"))) {
                    if sw % sh != 0 {
                        continue;
                    }
                    let mut frames = Vec::new();
                    for frame in 0..sw / sh {
                        let mut square = vec![0; (sh * sh * 4) as usize];
                        for y in 0..sh {
                            let src = ((y * sw + frame * sh) * 4) as usize;
                            let dst = (y * sh * 4) as usize;
                            square[dst..dst + (sh * 4) as usize]
                                .copy_from_slice(&p[src..src + (sh * 4) as usize]);
                        }
                        frames.push(resize(&square, sh, sh, w, h));
                    }
                    sheets.insert(state.to_string(), frames);
                }
            }
        }
        let mut out = Self {
            sheets,
            base,
            blink,
            pixels: vec![0; (w * h * 4) as usize],
            previous: vec![0; (w * h * 4) as usize],
            width: w,
            height: h,
            state: "idle".into(),
            time: 0.,
            state_time: 0.,
            transition: 1.,
            transition_duration: 0.18,
            idle_fps: config.animation.idle_fps.max(1) as f32,
            talking_fps: config.animation.talking_fps.max(1) as f32,
        };
        out.tick(Duration::ZERO);
        out
    }
    pub fn state_count(&self) -> usize {
        if self.base.is_some() {
            11
        } else {
            self.sheets.len()
        }
    }
    pub fn is_transitioning(&self) -> bool {
        self.transition < 1.
    }
    pub fn is_animated(&self) -> bool {
        self.base.is_some() || self.sheets.get(&self.state).is_some_and(|s| s.len() > 1)
    }
    pub fn transition_to(&mut self, state: &str, ms: u32) {
        if state == self.state {
            return;
        }
        self.previous.copy_from_slice(&self.pixels);
        self.state = state.to_string();
        self.state_time = 0.;
        self.transition_duration = ms as f32 / 1000.;
        self.transition = if ms == 0 { 1. } else { 0. };
    }
    pub fn tick(&mut self, dt: Duration) -> bool {
        let dt = dt.as_secs_f32();
        self.time += dt;
        self.state_time += dt;
        if self.transition_duration > 0. {
            self.transition = (self.transition + dt / self.transition_duration).min(1.);
        }
        if let Some(base) = &self.base {
            let t = self.time;
            // Blink only the registered eye patch; the body never changes identity.
            let blinking = self.state == "sleeping" || t % 4.7 > 4.53;
            let excited = matches!(self.state.as_str(), "happy" | "laughing" | "waving");
            let breath = (t * 1.8).sin() * 0.004;
            let tilt = match self.state.as_str() {
                "thinking" => -0.025,
                "sad" | "sleeping" => 0.018,
                "waving" => (self.state_time * 5.).sin() * 0.02,
                "angry" => -0.015,
                _ => (t * 0.7).sin() * 0.006,
            };
            let bounce = if excited {
                -(self.state_time * 4.).sin().abs() * 2.
            } else {
                0.
            };
            let surprise = if self.state == "surprised" {
                (-self.state_time * 4.).exp() * 0.04
            } else {
                0.
            };
            for y in 0..self.height {
                for x in 0..self.width {
                    let nx = x as f32 / self.width as f32;
                    let ny = (y as f32 - bounce) / self.height as f32;
                    // Feet remain planted; motion increases smoothly towards the head.
                    let sy = 0.94 - (0.94 - ny) / (1. + breath + surprise);
                    let sx = nx - tilt * (0.94 - sy).max(0.);
                    let mut color = sample(
                        base,
                        self.width,
                        self.height,
                        sx * self.width as f32,
                        sy * self.height as f32,
                    );
                    let face_x = (sx - 0.5) / 0.92 + 0.5;
                    let face_y = (sy - 0.5) / 0.92 + 0.5;
                    if blinking
                        && (0.395..0.59).contains(&face_x)
                        && (0.235..0.295).contains(&face_y)
                    {
                        if let Some(blink) = &self.blink {
                            color = sample(
                                blink,
                                self.width,
                                self.height,
                                sx * self.width as f32,
                                sy * self.height as f32,
                            );
                        }
                    }
                    // Small mouth aperture timed to speech state; no whole-pose frame jumping.
                    if self.state == "talking" || self.state == "laughing" {
                        let opening = (t * 15.).sin().max(0.) * 0.005;
                        if opening > 0.001
                            && ((face_x - 0.503) / 0.011).powi(2)
                                + ((face_y - 0.313) / opening).powi(2)
                                < 1.
                        {
                            color = [91, 43, 66, 255];
                        }
                    }
                    let i = ((y * self.width + x) * 4) as usize;
                    self.pixels[i..i + 4].copy_from_slice(&color);
                }
            }
        } else if let Some(frames) = self
            .sheets
            .get(&self.state)
            .or_else(|| self.sheets.get("idle"))
        {
            let fps = if self.state == "talking" {
                self.talking_fps
            } else {
                self.idle_fps
            };
            self.pixels
                .copy_from_slice(&frames[(self.state_time * fps) as usize % frames.len()]);
        }
        if self.transition < 1. {
            let t = self.transition * self.transition * (3. - 2. * self.transition);
            for (dst, old) in self
                .pixels
                .chunks_exact_mut(4)
                .zip(self.previous.chunks_exact(4))
            {
                let a = old[3] as f32 * (1. - t) + dst[3] as f32 * t;
                for c in 0..3 {
                    dst[c] = if a > 0. {
                        ((old[c] as f32 * old[3] as f32 * (1. - t)
                            + dst[c] as f32 * dst[3] as f32 * t)
                            / a) as u8
                    } else {
                        0
                    };
                }
                dst[3] = a as u8;
            }
        }
        true
    }
    pub fn current_frame(&self) -> Option<&[u8]> {
        Some(&self.pixels)
    }
}

fn load_png(path: &Path) -> Result<(Vec<u8>, u32, u32), String> {
    let mut decoder = png::Decoder::new(std::fs::File::open(path).map_err(|e| e.to_string())?);
    decoder.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut reader = decoder.read_info().map_err(|e| e.to_string())?;
    let mut buf = vec![0; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).map_err(|e| e.to_string())?;
    let channels = info.color_type.samples();
    let mut rgba = Vec::with_capacity((info.width * info.height * 4) as usize);
    for p in buf[..info.buffer_size()].chunks_exact(channels) {
        match info.color_type {
            png::ColorType::Rgba => rgba.extend_from_slice(p),
            png::ColorType::Rgb => rgba.extend_from_slice(&[p[0], p[1], p[2], 255]),
            png::ColorType::Grayscale => rgba.extend_from_slice(&[p[0], p[0], p[0], 255]),
            png::ColorType::GrayscaleAlpha => rgba.extend_from_slice(&[p[0], p[0], p[0], p[1]]),
            _ => return Err("Unsupported PNG".into()),
        }
    }
    Ok((rgba, info.width, info.height))
}

/// Bilinear interpolation in premultiplied space avoids dark alpha fringes.
fn sample(p: &[u8], w: u32, h: u32, x: f32, y: f32) -> [u8; 4] {
    let mut sum = [0.; 4];
    let fx = x.floor();
    let fy = y.floor();
    for (dy, wy) in [(0, 1. - (y - fy)), (1, y - fy)] {
        for (dx, wx) in [(0, 1. - (x - fx)), (1, x - fx)] {
            let sx = fx as i32 + dx;
            let sy = fy as i32 + dy;
            if sx < 0 || sy < 0 || sx >= w as i32 || sy >= h as i32 {
                continue;
            }
            let i = ((sy as u32 * w + sx as u32) * 4) as usize;
            let a = p[i + 3] as f32 * wx * wy;
            for c in 0..3 {
                sum[c] += p[i + c] as f32 * a;
            }
            sum[3] += a;
        }
    }
    if sum[3] <= 0. {
        return [0; 4];
    }
    [
        (sum[0] / sum[3]) as u8,
        (sum[1] / sum[3]) as u8,
        (sum[2] / sum[3]) as u8,
        sum[3] as u8,
    ]
}
fn resize(p: &[u8], sw: u32, sh: u32, w: u32, h: u32) -> Vec<u8> {
    let mut out = vec![0; (w * h * 4) as usize];
    let scale = (w as f32 / sw as f32).min(h as f32 / sh as f32) * 0.92;
    for y in 0..h {
        for x in 0..w {
            let sx = (x as f32 - w as f32 / 2.) / scale + sw as f32 / 2.;
            let sy = (y as f32 - h as f32 / 2.) / scale + sh as f32 / 2.;
            let i = ((y * w + x) * 4) as usize;
            let mut sum = [0f32; 4];
            let taps = if scale < 1. { 4 } else { 1 };
            for oy in 0..taps {
                for ox in 0..taps {
                    let dx = ((ox as f32 + 0.5) / taps as f32 - 0.5) / scale;
                    let dy = ((oy as f32 + 0.5) / taps as f32 - 0.5) / scale;
                    let color = sample(p, sw, sh, sx + dx, sy + dy);
                    for c in 0..3 {
                        sum[c] += color[c] as f32 * color[3] as f32;
                    }
                    sum[3] += color[3] as f32;
                }
            }
            if sum[3] > 0. {
                for c in 0..3 {
                    out[i + c] = (sum[c] / sum[3]) as u8;
                }
                out[i + 3] = (sum[3] / (taps * taps) as f32) as u8;
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn interpolation_preserves_color_at_alpha_edges() {
        let p = [255, 0, 0, 255, 0, 0, 0, 0];
        assert_eq!(sample(&p, 2, 1, 0.5, 0.), [255, 0, 0, 127]);
    }
    #[test]
    fn character_renders_at_non_square_sizes() {
        let mut config = OverlayConfig::default();
        config.window.width = 160;
        config.window.height = 220;
        let mut sprite = SpriteManager::load_all(Path::new("../assets/sprites"), &config);
        assert!(sprite.base.is_some());
        sprite.transition_to("laughing", 200);
        sprite.tick(Duration::from_millis(100));
        assert_eq!(sprite.current_frame().unwrap().len(), 160 * 220 * 4);
        assert!(sprite
            .current_frame()
            .unwrap()
            .chunks_exact(4)
            .any(|p| p[3] > 0));
    }
}
