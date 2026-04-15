//! Configuration loader for the overlay.
//!
//! Reads settings from `config/overlay.toml` including window position,
//! animation FPS, and speech bubble parameters.

use std::path::PathBuf;

use serde::Deserialize;

/// Top-level overlay configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct OverlayConfig {
    pub window: WindowConfig,
    pub animation: AnimationConfig,
    pub speech_bubble: SpeechBubbleConfig,
}

/// Window/surface configuration.
///
/// In Wayland layer-shell terms:
/// - `anchor` determines which screen edge the surface sticks to
/// - `margin_x/y` are the offsets from that anchor point
/// - `layer` is one of: background, bottom, top, overlay
#[derive(Debug, Clone, Deserialize)]
pub struct WindowConfig {
    #[serde(default = "default_size")]
    pub width: u32,
    #[serde(default = "default_size")]
    pub height: u32,
    #[serde(default = "default_anchor")]
    pub anchor: String,
    #[serde(default = "default_margin")]
    pub margin_x: i32,
    #[serde(default = "default_margin")]
    pub margin_y: i32,
    #[serde(default = "default_layer")]
    pub layer: String,
}

/// Animation timing configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct AnimationConfig {
    /// Frames per second for idle/sleeping states.
    #[serde(default = "default_idle_fps")]
    pub idle_fps: u32,
    /// Frames per second for talking/active states.
    #[serde(default = "default_talking_fps")]
    pub talking_fps: u32,
    /// Duration of crossfade transitions between states, in milliseconds.
    #[serde(default = "default_transition")]
    pub transition_ms: u32,
}

/// Speech bubble rendering configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct SpeechBubbleConfig {
    /// Maximum width of the speech bubble in pixels.
    #[serde(default = "default_max_width")]
    pub max_width: u32,
    /// Font size for speech text.
    #[serde(default = "default_font_size")]
    pub font_size: u32,
    /// Typewriter reveal speed in characters per second.
    #[serde(default = "default_typewriter_speed")]
    pub typewriter_speed: u32,
    /// Seconds to wait after text is fully revealed before dismissing.
    #[serde(default = "default_dismiss_delay")]
    pub dismiss_delay: f64,
}

// Default value functions for serde
fn default_size() -> u32 { 256 }
fn default_anchor() -> String { "bottom-right".to_string() }
fn default_margin() -> i32 { 50 }
fn default_layer() -> String { "overlay".to_string() }
fn default_idle_fps() -> u32 { 8 }
fn default_talking_fps() -> u32 { 12 }
fn default_transition() -> u32 { 200 }
fn default_max_width() -> u32 { 300 }
fn default_font_size() -> u32 { 14 }
fn default_typewriter_speed() -> u32 { 30 }
fn default_dismiss_delay() -> f64 { 3.0 }

impl OverlayConfig {
    /// Load configuration from the standard config path.
    pub fn load() -> Self {
        let config_path = Self::find_config_path();

        if let Some(path) = config_path {
            match std::fs::read_to_string(&path) {
                Ok(contents) => {
                    match toml::from_str(&contents) {
                        Ok(config) => {
                            log::info!("Loaded config from {}", path.display());
                            return config;
                        }
                        Err(e) => {
                            log::warn!("Failed to parse config {}: {}", path.display(), e);
                        }
                    }
                }
                Err(e) => {
                    log::warn!("Failed to read config {}: {}", path.display(), e);
                }
            }
        }

        log::info!("Using default configuration");
        Self::default()
    }

    /// Search for the config file in standard locations.
    fn find_config_path() -> Option<PathBuf> {
        // 1. Environment variable
        if let Ok(path) = std::env::var("VIRTUAL_BUDDY_CONFIG") {
            let p = PathBuf::from(path).join("overlay.toml");
            if p.exists() {
                return Some(p);
            }
        }

        // 2. Relative to executable (project layout)
        if let Ok(exe) = std::env::current_exe() {
            if let Some(project_root) = exe.parent().and_then(|p| p.parent()).and_then(|p| p.parent()) {
                let p = project_root.join("config").join("overlay.toml");
                if p.exists() {
                    return Some(p);
                }
            }
        }

        // 3. Current directory
        let p = PathBuf::from("config/overlay.toml");
        if p.exists() {
            return Some(p);
        }

        None
    }
}

impl Default for OverlayConfig {
    fn default() -> Self {
        Self {
            window: WindowConfig {
                width: 256,
                height: 256,
                anchor: "bottom-right".to_string(),
                margin_x: 50,
                margin_y: 50,
                layer: "overlay".to_string(),
            },
            animation: AnimationConfig {
                idle_fps: 8,
                talking_fps: 12,
                transition_ms: 200,
            },
            speech_bubble: SpeechBubbleConfig {
                max_width: 300,
                font_size: 14,
                typewriter_speed: 30,
                dismiss_delay: 3.0,
            },
        }
    }
}
