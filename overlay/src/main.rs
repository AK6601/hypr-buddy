//! Hypr Buddy Overlay
//! ======================
//!
//! A Wayland layer-shell overlay that renders an animated sprite character
//! with speech bubbles on CachyOS + Hyprland.
//!
//! # How It Works
//!
//! 1. Connect to the Wayland compositor (Hyprland) via `wayland-client`.
//! 2. Use the `wlr-layer-shell` protocol to create an overlay surface that
//!    floats above all windows without pushing them (exclusive_zone = -1).
//! 3. Render sprites + speech bubbles to an RGBA buffer, then convert to
//!    pre-multiplied ARGB8888 and write into a `wl_shm` shared-memory pool
//!    that the compositor reads from.
//! 4. Use `calloop` as the main event loop, integrating:
//!    - Wayland protocol dispatch (WaylandSource)
//!    - Animation timer (calloop::Timer)
//!    - IPC command listener (Unix domain socket via Generic source)
//! 5. Only request frame callbacks when something is animating — otherwise
//!    sleep to target < 2% CPU when idle.

mod config;
mod ipc;
mod renderer;
mod sprite;
mod speech;

use std::time::{Duration, Instant};

use calloop::timer::{TimeoutAction, Timer};
use calloop::EventLoop;
use log::{error, info, warn};

use smithay_client_toolkit as sctk;
use sctk::compositor::{CompositorHandler, CompositorState};
use sctk::output::{OutputHandler, OutputState};
use sctk::registry::{ProvidesRegistryState, RegistryState};
use sctk::registry_handlers;
use sctk::seat::{SeatHandler, SeatState};
use sctk::shell::wlr_layer::{
    Anchor, KeyboardInteractivity, Layer, LayerShell, LayerShellHandler, LayerSurface,
    LayerSurfaceConfigure,
};
use sctk::shell::WaylandSurface;
use sctk::shm::slot::SlotPool;
use sctk::shm::{Shm, ShmHandler};
use sctk::{
    delegate_compositor, delegate_layer, delegate_output, delegate_registry, delegate_seat,
    delegate_shm,
};
use wayland_client::globals::registry_queue_init;
use wayland_client::protocol::{wl_output, wl_seat, wl_shm, wl_surface};
use wayland_client::{Connection, QueueHandle};

use config::OverlayConfig;
use ipc::{IpcCommand, IpcServer};
use renderer::ShmRenderer;
use speech::SpeechBubble;
use sprite::SpriteManager;

// ---------------------------------------------------------------------------
// Application State
// ---------------------------------------------------------------------------

/// Holds all state: Wayland protocol objects, animation, sprites, IPC.
struct BuddyApp {
    // --- Wayland protocol state (required by SCTK delegate macros) ---
    registry_state: RegistryState,
    seat_state: SeatState,
    output_state: OutputState,
    compositor_state: CompositorState,
    shm: Shm,

    // --- Layer shell surface ---
    layer_shell: LayerShell,
    layer_surface: Option<LayerSurface>,
    /// Shared-memory buffer pool for wl_shm rendering.
    pool: Option<SlotPool>,

    // --- Display state ---
    width: u32,
    height: u32,
    /// Set to true after the compositor sends the first configure event.
    configured: bool,

    // --- Animation / app logic ---
    config: OverlayConfig,
    sprites: SpriteManager,
    speech: Option<SpeechBubble>,
    renderer: ShmRenderer,
    current_state: String,
    revert_state: Option<(String, Instant)>,
    current_x: f64,
    current_y: f64,
    target_x: f64,
    target_y: f64,
    visible: bool,
    needs_redraw: bool,
    exit: bool,
    last_frame: Instant,
}

impl BuddyApp {
    /// Process an IPC command from the brain.
    fn handle_command(&mut self, cmd: IpcCommand) {
        match cmd {
            IpcCommand::SetState { state, duration } => {
                info!("State -> {} (duration: {}s)", state, duration);
                self.current_state = state;
                if duration > 0.0 {
                    self.revert_state = Some((
                        "idle".to_string(),
                        Instant::now() + Duration::from_secs_f64(duration),
                    ));
                } else {
                    self.revert_state = None;
                }
                self.sprites
                    .transition_to(&self.current_state, self.config.animation.transition_ms);
                self.needs_redraw = true;
            }
            IpcCommand::Say { text, state } => {
                info!("Say: '{}' (state: {})", text, state);
                self.speech = Some(SpeechBubble::new(
                    text,
                    self.config.speech_bubble.typewriter_speed,
                    self.config.speech_bubble.dismiss_delay,
                    self.config.speech_bubble.max_width,
                    self.config.speech_bubble.font_size,
                ));
                self.current_state = state;
                self.sprites
                    .transition_to(&self.current_state, self.config.animation.transition_ms);
                self.needs_redraw = true;
            }
            IpcCommand::Move { x, y } => {
                info!("Target Move to: {}, {}", x, y);
                self.target_x = x as f64;
                self.target_y = y as f64;
                // If this is the first move, teleport immediately
                if self.current_x == 0.0 && self.current_y == 0.0 {
                    self.current_x = self.target_x;
                    self.current_y = self.target_y;
                }
            }
            IpcCommand::Visibility { visible } => {
                info!("Visibility: {}", visible);
                self.visible = visible;
                self.needs_redraw = true;
            }
            IpcCommand::Quit => {
                info!("Quit command received");
                self.exit = true;
            }
        }
    }

    /// Advance animation and speech state. Returns true if a redraw is needed.
    fn tick(&mut self) -> bool {
        let now = Instant::now();
        let dt = now.duration_since(self.last_frame);
        self.last_frame = now;

        // --- 1. Movement Logic ---
        // Smooth movement interpolation (lerp)
        let lerp_factor = 0.15;
        let dx = self.target_x - self.current_x;
        let dy = self.target_y - self.current_y;
        let mut movement_dirty = false;

        if dx.abs() > 0.1 || dy.abs() > 0.1 {
            self.current_x += dx * lerp_factor;
            self.current_y += dy * lerp_factor;
            movement_dirty = true;
        } else {
            // Snap to target if very close
            self.current_x = self.target_x;
            self.current_y = self.target_y;
        }

        // --- 2. Idle "Floating" Effect ---
        // Adds a subtle up/down wave when idle to make her feel alive
        let (float_x, float_y) = if self.current_state == "idle" || self.current_state == "waving" {
            let t = now.duration_since(Instant::now() - Duration::from_secs(3600)).as_secs_f64();
            (
                (t * 0.5).cos() * 2.0, // 2px side-to-side
                (t * 1.5).sin() * 4.0, // 4px up-and-down
            )
        } else {
            (0.0, 0.0)
        };

        if movement_dirty || float_x.abs() > 0.1 || float_y.abs() > 0.1 {
            if let Some(ref ls) = self.layer_surface {
                ls.set_anchor(Anchor::TOP | Anchor::LEFT);
                ls.set_margin(
                    (self.current_y + float_y) as i32,
                    0,
                    0,
                    (self.current_x + float_x) as i32,
                );
                // We don't commit here anymore, draw() will do it
            }
            movement_dirty = true;
        }

        // --- 3. State/Animation Logic ---
        // Check state revert timer
        if let Some((ref revert_to, deadline)) = self.revert_state {
            if now >= deadline {
                let revert = revert_to.clone();
                self.current_state = revert;
                self.sprites
                    .transition_to(&self.current_state, self.config.animation.transition_ms);
                self.revert_state = None;
                self.needs_redraw = true;
            }
        }

        let sprite_dirty = self.sprites.tick(dt);

        let speech_dirty = if let Some(ref mut bubble) = self.speech {
            let dirty = bubble.tick(dt);
            if bubble.is_dismissed() {
                self.speech = None;
                if self.current_state == "talking" {
                    self.current_state = "idle".to_string();
                    self.sprites.transition_to("idle", self.config.animation.transition_ms);
                }
                true
            } else {
                dirty
            }
        } else {
            false
        };

        self.last_frame = now;
        let dirty = sprite_dirty || speech_dirty || self.needs_redraw || movement_dirty;
        self.needs_redraw = false;
        dirty
    }

    fn draw(&mut self, qh: &QueueHandle<Self>) {
        let layer_surface = match self.layer_surface.as_ref() {
            Some(ls) => ls,
            None => return,
        };
        let pool = match self.pool.as_mut() {
            Some(p) => p,
            None => return,
        };

        if !self.visible {
            layer_surface.wl_surface().attach(None, 0, 0);
            layer_surface.wl_surface().commit();
            return;
        }

        let width = self.width;
        let height = self.height;
        let stride = width as i32 * 4;

        let (buffer, canvas) = match pool.create_buffer(
            width as i32,
            height as i32,
            stride,
            wl_shm::Format::Argb8888,
        ) {
            Ok(pair) => pair,
            Err(e) => {
                warn!("Failed to create wl_shm buffer: {}", e);
                return;
            }
        };

        self.renderer.clear();
        if let Some(pixels) = self.sprites.current_frame() {
            self.renderer.blit_sprite(pixels, width, height);
        }
        if let Some(ref bubble) = self.speech {
            let (bpx, bx, by, bw, bh) = bubble.render(width);
            self.renderer.blit_overlay(&bpx, bx, by, bw, bh);
        }

        let rgba = self.renderer.pixels();
        for i in 0..(width * height) as usize {
            let src = i * 4;
            let dst = i * 4;
            let a = rgba[src + 3] as u32;
            canvas[dst] = (rgba[src + 2] as u32 * a / 255) as u8;     // B
            canvas[dst + 1] = (rgba[src + 1] as u32 * a / 255) as u8; // G
            canvas[dst + 2] = (rgba[src] as u32 * a / 255) as u8;     // R
            canvas[dst + 3] = a as u8;                               // A
        }

        let surface = layer_surface.wl_surface();
        surface.attach(Some(buffer.wl_buffer()), 0, 0);
        surface.damage_buffer(0, 0, width as i32, height as i32);

        // ALWAYS request a frame callback if we are animating or moving.
        // This ensures tick() is called at the display's refresh rate.
        let is_moving = (self.target_x - self.current_x).abs() > 0.1 || (self.target_y - self.current_y).abs() > 0.1;
        let is_floating = self.current_state == "idle" || self.current_state == "waving";
        
        if self.speech.is_some() || self.sprites.is_transitioning() || is_moving || is_floating {
            surface.frame(qh, surface.clone());
        }

        surface.commit();
    }
}

// ---------------------------------------------------------------------------
// SCTK Handler Implementations
// ---------------------------------------------------------------------------
// These traits are required by SCTK's delegate macros. Each one handles
// events from a specific Wayland protocol extension.

impl CompositorHandler for BuddyApp {
    fn scale_factor_changed(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _surface: &wl_surface::WlSurface,
        _new_factor: i32,
    ) {
        // TODO: handle HiDPI scaling
    }

    fn transform_changed(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _surface: &wl_surface::WlSurface,
        _new_transform: wl_output::Transform,
    ) {
    }

    fn surface_enter(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _surface: &wl_surface::WlSurface,
        _output: &wl_output::WlOutput,
    ) {
    }

    fn surface_leave(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _surface: &wl_surface::WlSurface,
        _output: &wl_output::WlOutput,
    ) {
    }

    /// Called by the compositor when it's ready for a new frame.
    /// This is the VSync callback — we render and commit here.
    fn frame(
        &mut self,
        _conn: &Connection,
        qh: &QueueHandle<Self>,
        _surface: &wl_surface::WlSurface,
        _time: u32,
    ) {
        let dirty = self.tick();
        if dirty {
            self.draw(qh);
        }
    }
}

impl OutputHandler for BuddyApp {
    fn output_state(&mut self) -> &mut OutputState {
        &mut self.output_state
    }

    fn new_output(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _output: wl_output::WlOutput,
    ) {
    }

    fn update_output(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _output: wl_output::WlOutput,
    ) {
    }

    fn output_destroyed(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _output: wl_output::WlOutput,
    ) {
    }
}

impl LayerShellHandler for BuddyApp {
    /// Called when the compositor wants to close our surface.
    fn closed(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _layer: &LayerSurface,
    ) {
        info!("Layer surface closed by compositor");
        self.exit = true;
    }

    /// Called when the compositor configures our surface (initial and on resize).
    ///
    /// The first configure is special — we must acknowledge it and draw our
    /// first frame before the surface becomes visible.
    fn configure(
        &mut self,
        _conn: &Connection,
        qh: &QueueHandle<Self>,
        layer: &LayerSurface,
        configure: LayerSurfaceConfigure,
        _serial: u32,
    ) {
        // The compositor may suggest a size (0 means "you choose")
        if configure.new_size.0 != 0 {
            self.width = configure.new_size.0;
        }
        if configure.new_size.1 != 0 {
            self.height = configure.new_size.1;
        }

        info!("Configure: {}x{}", self.width, self.height);

        // Recreate renderer if size changed
        self.renderer = ShmRenderer::new(self.width, self.height);

        // set_margin(top, right, bottom, left)
        let (margin_top, margin_right, margin_bottom, margin_left) =
            match self.config.window.anchor.as_str() {
                "bottom-right" => (0, self.config.window.margin_x, self.config.window.margin_y, 0),
                "bottom-left" => (0, 0, self.config.window.margin_y, self.config.window.margin_x),
                "top-right" => (self.config.window.margin_y, self.config.window.margin_x, 0, 0),
                "top-left" => (self.config.window.margin_y, 0, 0, self.config.window.margin_x),
                _ => (0, self.config.window.margin_x, self.config.window.margin_y, 0),
            };

        if !self.configured {
            self.configured = true;
            self.current_x = margin_left as f64;
            self.current_y = margin_top as f64;
            self.target_x = self.current_x;
            self.target_y = self.current_y;
            // Draw the first frame immediately
            self.draw(qh);
        }
 else {
            // A commit is required after configure even if we don't redraw
            layer.wl_surface().commit();
        }
    }
}

impl SeatHandler for BuddyApp {
    fn seat_state(&mut self) -> &mut SeatState {
        &mut self.seat_state
    }

    fn new_seat(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _seat: wl_seat::WlSeat,
    ) {
    }

    fn new_capability(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _seat: wl_seat::WlSeat,
        _capability: sctk::seat::Capability,
    ) {
    }

    fn remove_capability(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _seat: wl_seat::WlSeat,
        _capability: sctk::seat::Capability,
    ) {
    }

    fn remove_seat(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        _seat: wl_seat::WlSeat,
    ) {
    }
}

impl ShmHandler for BuddyApp {
    fn shm_state(&mut self) -> &mut Shm {
        &mut self.shm
    }
}

// --- SCTK delegate macros ---
// These generate the Dispatch impls that route Wayland protocol events
// to our handler methods above.
delegate_compositor!(BuddyApp);
delegate_output!(BuddyApp);
delegate_layer!(BuddyApp);
delegate_seat!(BuddyApp);
delegate_shm!(BuddyApp);
delegate_registry!(BuddyApp);

impl ProvidesRegistryState for BuddyApp {
    fn registry(&mut self) -> &mut RegistryState {
        &mut self.registry_state
    }
    registry_handlers![OutputState, SeatState];
}

// ---------------------------------------------------------------------------
// Entry Point
// ---------------------------------------------------------------------------

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    info!("Hypr Buddy Overlay starting...");

    let config = OverlayConfig::load();
    info!(
        "Config: {}x{}, anchor={}, layer={}",
        config.window.width, config.window.height, config.window.anchor, config.window.layer
    );

    // --- Start IPC server ---
    let runtime_dir = std::env::var("XDG_RUNTIME_DIR")
        .unwrap_or_else(|_| format!("/run/user/{}", unsafe { libc::getuid() }));
    let buddy_dir = format!("{}/hypr-buddy", runtime_dir);
    let _ = std::fs::create_dir_all(&buddy_dir);
    let ipc_path = format!("{}/overlay.sock", buddy_dir);

    let mut ipc = match IpcServer::new(&ipc_path) {
        Ok(s) => {
            info!("IPC listening on {}", ipc_path);
            s
        }
        Err(e) => {
            error!("Failed to start IPC: {}", e);
            std::process::exit(1);
        }
    };

    // --- Connect to Wayland ---
    let conn = match Connection::connect_to_env() {
        Ok(c) => c,
        Err(e) => {
            error!("Cannot connect to Wayland display: {}", e);
            error!("Make sure Hyprland is running and WAYLAND_DISPLAY is set.");
            std::process::exit(1);
        }
    };

    let (globals, event_queue) = match registry_queue_init(&conn) {
        Ok(pair) => pair,
        Err(e) => {
            error!("Failed to get Wayland registry: {}", e);
            std::process::exit(1);
        }
    };
    let qh = event_queue.handle();

    // --- Initialize SCTK protocol states from the global registry ---
    let compositor_state =
        CompositorState::bind(&globals, &qh).expect("wl_compositor not available");
    let layer_shell = LayerShell::bind(&globals, &qh).expect(
        "wlr-layer-shell not available — your compositor must support \
         wlr-layer-shell-unstable-v1 (Hyprland does)",
    );
    let shm = Shm::bind(&globals, &qh).expect("wl_shm not available");
    let output_state = OutputState::new(&globals, &qh);
    let seat_state = SeatState::new(&globals, &qh);
    let registry_state = RegistryState::new(&globals);

    // --- Create the overlay surface ---
    let surface = compositor_state.create_surface(&qh);

    // Parse anchor from config
    let anchor = match config.window.anchor.as_str() {
        "bottom-right" => Anchor::BOTTOM | Anchor::RIGHT,
        "bottom-left" => Anchor::BOTTOM | Anchor::LEFT,
        "top-right" => Anchor::TOP | Anchor::RIGHT,
        "top-left" => Anchor::TOP | Anchor::LEFT,
        _ => Anchor::BOTTOM | Anchor::RIGHT,
    };

    // Parse layer from config
    let layer = match config.window.layer.as_str() {
        "overlay" => Layer::Overlay,
        "top" => Layer::Top,
        "bottom" => Layer::Bottom,
        "background" => Layer::Background,
        _ => Layer::Overlay,
    };

    let layer_surface =
        layer_shell.create_layer_surface(&qh, surface, layer, Some("hypr-buddy"), None);

    // Configure the layer surface:
    // - anchor: which corner of the screen
    // - size: fixed pixel dimensions
    // - exclusive_zone: -1 means "don't reserve space, float over windows"
    // - keyboard_interactivity: None means we don't steal keyboard focus
    // - margin: offset from the anchor edge
    layer_surface.set_anchor(anchor);
    layer_surface.set_size(config.window.width, config.window.height);
    layer_surface.set_exclusive_zone(-1);
    layer_surface.set_keyboard_interactivity(KeyboardInteractivity::None);

    // set_margin(top, right, bottom, left)
    let (margin_top, margin_right, margin_bottom, margin_left) =
        match config.window.anchor.as_str() {
            "bottom-right" => (0, config.window.margin_x, config.window.margin_y, 0),
            "bottom-left" => (0, 0, config.window.margin_y, config.window.margin_x),
            "top-right" => (config.window.margin_y, config.window.margin_x, 0, 0),
            "top-left" => (config.window.margin_y, 0, 0, config.window.margin_x),
            _ => (0, config.window.margin_x, config.window.margin_y, 0),
        };
    layer_surface.set_margin(margin_top, margin_right, margin_bottom, margin_left);

    // The initial commit triggers the compositor to send us a configure event.
    layer_surface.commit();

    // --- Create shared memory buffer pool ---
    let pool_size = (config.window.width * config.window.height * 4) as usize;
    let pool = SlotPool::new(pool_size, &shm).expect("Failed to create wl_shm pool");

    // --- Load sprites ---
    let sprites_dir = std::env::var("HYPR_BUDDY_ASSETS").unwrap_or_else(|_| {
        let exe_dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|p| p.to_path_buf()))
            .unwrap_or_else(|| std::path::PathBuf::from("."));
        let project_root = exe_dir
            .parent()
            .and_then(|p| p.parent())
            .unwrap_or(&exe_dir);
        project_root
            .join("assets")
            .join("sprites")
            .to_string_lossy()
            .into_owned()
    });

    let sprites = SpriteManager::load_all(std::path::Path::new(&sprites_dir), &config);
    info!("Loaded {} sprite states", sprites.state_count());

    let renderer = ShmRenderer::new(config.window.width, config.window.height);

    // --- Build app state ---
    let mut app = BuddyApp {
        registry_state,
        seat_state,
        output_state,
        compositor_state,
        shm,
        layer_shell,
        layer_surface: Some(layer_surface),
        pool: Some(pool),
        width: config.window.width,
        height: config.window.height,
        configured: false,
        config,
        sprites,
        speech: None,
        renderer,
        current_state: "idle".to_string(),
        revert_state: None,
        visible: true,
        current_x: 0.0,
        current_y: 0.0,
        target_x: 0.0,
        target_y: 0.0,
        needs_redraw: true,
        exit: false,
        last_frame: Instant::now(),
    };

    // --- Set up calloop event loop ---
    let mut event_loop: EventLoop<BuddyApp> =
        EventLoop::try_new().expect("Failed to create event loop");
    let loop_handle = event_loop.handle();

    // Insert the Wayland event source — this dispatches Wayland protocol
    // events to our handler implementations above.
    sctk::reexports::calloop_wayland_source::WaylandSource::new(conn.clone(), event_queue)
        .insert(loop_handle.clone())
        .expect("Failed to insert Wayland source into event loop");

    // Use a calloop channel to forward IPC commands into the event loop
    // so we can process them with access to the QueueHandle.
    let (ipc_tx, ipc_rx) = calloop::channel::channel::<IpcCommand>();
    loop_handle
        .insert_source(ipc_rx, |event, _, state: &mut BuddyApp| {
            if let calloop::channel::Event::Msg(cmd) = event {
                state.handle_command(cmd);
            }
        })
        .expect("Failed to insert IPC channel source");

    // Animation timer — fires periodically to advance animation and redraw.
    let timer = Timer::from_duration(Duration::from_millis(100));
    let qh_for_timer = qh.clone();
    loop_handle
        .insert_source(timer, move |_, _, state: &mut BuddyApp| {
            let dirty = state.tick();

            // Determine next tick interval based on activity
            let interval = if state.speech.is_some() || state.sprites.is_transitioning() {
                let fps = state.config.animation.talking_fps.max(1);
                Duration::from_secs_f64(1.0 / fps as f64)
            } else if state.current_state == "idle" || state.current_state == "sleeping" {
                // Idle: tick slowly to save CPU
                Duration::from_millis(250)
            } else {
                let fps = state.config.animation.idle_fps.max(1);
                Duration::from_secs_f64(1.0 / fps as f64)
            };

            if (dirty || state.needs_redraw) && state.configured {
                state.draw(&qh_for_timer);
                state.needs_redraw = false;
            }

            TimeoutAction::ToDuration(interval)
        })
        .expect("Failed to insert timer source");

    // --- Main event loop ---
    info!("Entering main event loop");

    while !app.exit {
        // Dispatch all pending events with a short timeout.
        if let Err(e) = event_loop.dispatch(Duration::from_millis(16), &mut app) {
            error!("Event loop error: {}", e);
            break;
        }

        // Poll IPC for commands and forward them into the calloop event loop
        match ipc.poll() {
            Ok(Some(cmd)) => {
                let _ = ipc_tx.send(cmd);
            }
            Ok(None) => {}
            Err(e) => warn!("IPC error: {}", e),
        }
    }

    drop(ipc);
    info!("Overlay stopped.");
}
