//! Reproducible, headless animation preview using the production renderer.
use crate::{
    config::OverlayConfig, renderer::ShmRenderer, speech::SpeechBubble, sprite::SpriteManager,
};
use std::{fs::File, path::Path, time::Duration};

pub fn render(path: &Path, config: &OverlayConfig) -> Result<(), Box<dyn std::error::Error>> {
    let dir = std::env::var("HYPR_BUDDY_ASSETS").unwrap_or_else(|_| "assets/sprites".into());
    let mut sprites = SpriteManager::load_all(Path::new(&dir), config);
    let mut canvas = ShmRenderer::new(400, 420);
    let mut bubble = SpeechBubble::new("Hey. I'm here when you need me.".into(), 60, 30., 300, 15);
    bubble.tick(Duration::from_secs(1));
    let mut encoder = png::Encoder::new(File::create(path)?, 400, 420);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    encoder.set_animated(150, 0)?;
    encoder.set_frame_delay(1, 30)?;
    let mut writer = encoder.write_header()?;
    for n in 0..150 {
        if n == 30 {
            sprites.transition_to("talking", 180);
        }
        if n == 90 {
            sprites.transition_to("idle", 180);
        }
        sprites.tick(Duration::from_secs_f64(1. / 30.));
        bubble.tick(Duration::from_secs_f64(1. / 30.));
        canvas.clear();
        canvas.blit_sprite_at(
            sprites.current_frame().unwrap(),
            config.window.width,
            config.window.height,
            88,
            170,
        );
        let (pixels, _, _, w, h) = bubble.render(config.window.width);
        canvas.blit_overlay(&pixels, 50, 160 - h as i32, w, h);
        writer.write_image_data(canvas.pixels())?;
    }
    writer.finish()?;
    Ok(())
}
