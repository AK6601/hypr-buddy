//! Hypr Buddy Overlay
//! ======================
//!
//! A Wayland layer-shell overlay that renders an animated sprite character
//! with speech bubbles on CachyOS + Hyprland.
//!
//! # How It Works
//!
//! 1. Connect to the Wayland compositor (Hyprland) via `wayland-client`.
//! 2. Use the `wlr-layer-shell` protocol to create ONE transparent overlay
//!    surface PER `wl_output`, each covering its whole output
//!    (anchored to all four edges, size 0x0 so the compositor fills it) and
//!    floating above all windows without reserving space (exclusive_zone = -1).
//!    An EMPTY input region makes every surface click-through.
//! 3. The buddy has a single GLOBAL position (Hyprland compositor-space
//!    coordinates, matching what the brain sends via the `move` IPC command).
//!    For each output we translate that global position into output-local
//!    coordinates and blit the sprite there — so moving the character is just
//!    changing where we draw inside the full-output buffer. We never call
//!    `set_margin`/`set_anchor` for movement anymore (that caused jitter).
//! 4. Render sprites + speech bubbles to an RGBA buffer, then convert to
//!    pre-multiplied ARGB8888 and write into a `wl_shm` shared-memory pool
//!    that the compositor reads from.
//! 5. Use `calloop` as the main event loop, integrating:
//!    - Wayland protocol dispatch (WaylandSource)
//!    - Animation timer (calloop::Timer)
//!    - IPC command listener (Unix domain socket via Generic source)
//! 6. One bounded animation clock drives all outputs, avoiding callback fan-out.

mod config;
mod ipc;
mod preview;
mod renderer;
mod speech;
mod sprite;

use std::time::{Duration, Instant};

use calloop::timer::{TimeoutAction, Timer};
use calloop::EventLoop;
use log::{error, info, warn};

use sctk::compositor::{CompositorHandler, CompositorState, Region};
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
use smithay_client_toolkit as sctk;
use wayland_client::globals::registry_queue_init;
use wayland_client::protocol::{wl_output, wl_seat, wl_shm, wl_surface};
use wayland_client::{Connection, QueueHandle};

use config::OverlayConfig;
use ipc::{IpcCommand, IpcServer};
use renderer::ShmRenderer;
use speech::SpeechBubble;
use sprite::SpriteManager;

// ---------------------------------------------------------------------------
// Per-output surface
// ---------------------------------------------------------------------------

/// One full-output transparent layer surface plus everything needed to draw
/// the sprite onto it.
///
/// We create exactly one of these per `wl_output`. Each surface covers its
/// entire output; we paint the (globally-positioned) sprite into it at the
/// correct output-local offset, or leave it fully transparent if the sprite
/// isn't currently over this output.
struct OutputSurface {
    /// The `wl_output` this surface is pinned to.
    output: wl_output::WlOutput,
    /// The layer-shell surface (full output, click-through).
    layer: LayerSurface,
    /// This surface's own shared-memory pool (sized to the output).
    pool: SlotPool,
    /// Per-surface software renderer (buffer == output pixel size).
    renderer: ShmRenderer,
    /// The empty input region. Kept alive for the lifetime of the surface so
    /// the compositor keeps routing clicks through us. Dropping it destroys
    /// the wl_region.
    _input_region: Region,

    /// Output origin in GLOBAL compositor space (logical_position, fallback 0,0).
    origin_x: i32,
    origin_y: i32,

    /// Current buffer size in pixels (from the compositor's configure).
    width: u32,
    height: u32,

    /// Set true once the compositor has sent the first configure for this
    /// surface (we must not attach a buffer before that).
    configured: bool,

    /// Bounding rect (output-local) damaged by the *previous* frame, so the
    /// next frame can clear it. `(x, y, w, h)`; `w == 0` means "nothing drawn".
    prev_rect: (i32, i32, u32, u32),
}

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

    // --- Layer shell ---
    layer_shell: LayerShell,
    /// The wlr-layer-shell layer (overlay/top/...) parsed from config once.
    layer: Layer,
    /// One surface per output. Index has no meaning beyond iteration.
    surfaces: Vec<OutputSurface>,

    // --- Animation / app logic ---
    config: OverlayConfig,
    sprites: SpriteManager,
    speech: Option<SpeechBubble>,
    current_state: String,
    revert_state: Option<(String, Instant)>,
    /// Buddy GLOBAL position (top-left of the sprite box), lerped toward target.
    current_x: f64,
    current_y: f64,
    target_x: f64,
    target_y: f64,
    /// True until the very first `move` command, so we can teleport instead of
    /// lerping from (0,0) the first time.
    have_target: bool,
    /// Current idle-float offset (render-space only), updated each tick.
    float_x: f64,
    float_y: f64,
    visible: bool,
    needs_redraw: bool,
    exit: bool,
    last_frame: Instant,
    /// Process start, used as the phase origin for the idle float wobble.
    start_time: Instant,
}

impl BuddyApp {
    /// Process an IPC command from the brain.
    fn handle_command(&mut self, cmd: IpcCommand) {
        match cmd {
            IpcCommand::SetState { state, duration } => {
                info!("State -> {} (duration: {}s)", state, duration);
                self.current_state = state;
                if self.current_state == "thinking" {
                    self.speech = None;
                }
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
                self.revert_state = None;
                self.sprites
                    .transition_to(&self.current_state, self.config.animation.transition_ms);
                self.needs_redraw = true;
            }
            IpcCommand::Move { x, y } => {
                info!("Target Move to: {}, {} (global)", x, y);
                self.target_x = x as f64;
                self.target_y = y as f64;
                // First move teleports (don't lerp in from the resting corner);
                // subsequent moves lerp smoothly toward the new target.
                if !self.have_target {
                    self.current_x = self.target_x;
                    self.current_y = self.target_y;
                    self.have_target = true;
                }
                self.needs_redraw = true;
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

    /// Create a full-output transparent, click-through layer surface for the
    /// given `wl_output` and push it onto `self.surfaces`.
    ///
    /// Skips the output if we already have a surface for it.
    fn create_output_surface(&mut self, output: wl_output::WlOutput, qh: &QueueHandle<Self>) {
        if self.surfaces.iter().any(|s| s.output == output) {
            return; // already have a surface for this output
        }

        // Logical geometry of the output in global compositor space.
        let info = self.output_state.info(&output);
        let (origin_x, origin_y) = info
            .as_ref()
            .and_then(|i| i.logical_position)
            .unwrap_or((0, 0));
        // Initial pixel size guess from logical_size; the compositor will tell
        // us the authoritative size in configure(). 0x0 lets it pick.
        let (init_w, init_h) = info
            .as_ref()
            .and_then(|i| i.logical_size)
            .map(|(w, h)| (w.max(1) as u32, h.max(1) as u32))
            .unwrap_or((1, 1));

        let name = info
            .as_ref()
            .and_then(|i| i.name.clone())
            .unwrap_or_else(|| "<unknown>".to_string());
        info!(
            "Creating overlay surface for output '{}' origin=({},{}) size~=({}x{})",
            name, origin_x, origin_y, init_w, init_h
        );

        // Create the wl_surface + layer surface, pinned to THIS output.
        let wl_surface = self.compositor_state.create_surface(qh);

        // EMPTY input region => every click passes through the fullscreen
        // overlay to the windows beneath it. We add nothing to the region.
        let input_region = match Region::new(&self.compositor_state) {
            Ok(r) => r,
            Err(e) => {
                error!("Failed to create input region: {}", e);
                return;
            }
        };
        wl_surface.set_input_region(Some(input_region.wl_region()));

        let layer = self.layer_shell.create_layer_surface(
            qh,
            wl_surface,
            self.layer,
            Some("hypr-buddy"),
            Some(&output),
        );

        // Cover the whole output: anchor to all four edges + size 0x0 (let the
        // compositor fill it) + don't reserve space + no keyboard focus.
        layer.set_anchor(Anchor::TOP | Anchor::BOTTOM | Anchor::LEFT | Anchor::RIGHT);
        layer.set_size(0, 0);
        layer.set_exclusive_zone(-1);
        layer.set_keyboard_interactivity(KeyboardInteractivity::None);

        // Initial commit -> triggers the first configure from the compositor.
        layer.commit();

        // One pool + renderer per output, sized to the (initial) output size.
        let pool_size = (init_w * init_h * 4) as usize;
        let pool = match SlotPool::new(pool_size.max(4), &self.shm) {
            Ok(p) => p,
            Err(e) => {
                error!("Failed to create wl_shm pool for output: {}", e);
                return;
            }
        };
        let renderer = ShmRenderer::new(init_w, init_h);

        self.surfaces.push(OutputSurface {
            output,
            layer,
            pool,
            renderer,
            _input_region: input_region,
            origin_x,
            origin_y,
            width: init_w,
            height: init_h,
            configured: false,
            prev_rect: (0, 0, 0, 0),
        });
    }

    /// Refresh the cached origin (and, if logical_size is known, treat it as a
    /// hint) for an output whose geometry changed.
    fn refresh_output_geometry(&mut self, output: &wl_output::WlOutput) {
        let info = match self.output_state.info(output) {
            Some(i) => i,
            None => return,
        };
        if let Some(surf) = self.surfaces.iter_mut().find(|s| &s.output == output) {
            if let Some((ox, oy)) = info.logical_position {
                surf.origin_x = ox;
                surf.origin_y = oy;
            }
        }
    }

    /// Is the buddy currently moving (lerping toward a new target)?
    fn is_moving(&self) -> bool {
        (self.target_x - self.current_x).abs() > 0.1 || (self.target_y - self.current_y).abs() > 0.1
    }

    /// Should the idle float effect be active right now?
    fn is_floating(&self) -> bool {
        self.config.window.float && (self.current_state == "idle" || self.current_state == "waving")
    }

    /// True when nothing visual is in motion: no movement, no float, no speech,
    /// no sprite transition. When fully idle we request no frame callbacks and
    /// tick slowly so idle CPU stays ~0.
    fn is_fully_idle(&self) -> bool {
        !self.is_moving()
            && !self.is_floating()
            && self.speech.is_none()
            && !self.sprites.is_transitioning()
            && !self.sprites.is_animated()
    }

    /// Advance animation and speech state. Returns true if a redraw is needed.
    fn tick(&mut self) -> bool {
        let now = Instant::now();
        let dt = now.duration_since(self.last_frame);
        self.last_frame = now;

        // --- 1. Movement Logic (pure render-space; lerp the GLOBAL position) ---
        let lerp_factor = 1.0 - (-12.0 * dt.as_secs_f64().min(0.1)).exp();
        let dx = self.target_x - self.current_x;
        let dy = self.target_y - self.current_y;
        let mut movement_dirty = false;

        if dx.abs() > 0.1 || dy.abs() > 0.1 {
            self.current_x += dx * lerp_factor;
            self.current_y += dy * lerp_factor;
            movement_dirty = true;
        } else {
            // Snap to target if very close.
            self.current_x = self.target_x;
            self.current_y = self.target_y;
        }

        // --- 2. Idle "Floating" Effect (pure render-space offset) ---
        // A subtle wobble while idle. This only changes WHERE we blit the
        // sprite inside the full-output buffer — we never touch set_margin /
        // set_anchor, so there is no compositor reconfigure and no jitter.
        let prev_float = (self.float_x, self.float_y);
        let (float_x, float_y) = if self.is_floating() {
            let t = now.duration_since(self.start_time).as_secs_f64();
            (
                (t * 0.5).cos() * 2.0, // 2px side-to-side
                (t * 1.5).sin() * 4.0, // 4px up-and-down
            )
        } else {
            (0.0, 0.0)
        };
        self.float_x = float_x;
        self.float_y = float_y;
        let float_dirty =
            (float_x - prev_float.0).abs() > 0.01 || (float_y - prev_float.1).abs() > 0.01;

        // --- 3. State/Animation Logic ---
        // Check state revert timer.
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
                if self.current_state == "talking" && self.revert_state.is_none() {
                    self.current_state = "idle".to_string();
                    self.sprites
                        .transition_to("idle", self.config.animation.transition_ms);
                }
                true
            } else {
                dirty
            }
        } else {
            false
        };

        let dirty =
            sprite_dirty || speech_dirty || self.needs_redraw || movement_dirty || float_dirty;
        self.needs_redraw = false;
        dirty
    }

    /// Render every output surface for the current frame.
    fn draw(&mut self, _qh: &QueueHandle<Self>) {
        // Whether a frame callback should be requested (something is still in
        // motion and we want to be woken again at vsync). Computed once.

        // Sprite box size (always the configured logical sprite size).
        let sprite_w = self.config.window.width;
        let sprite_h = self.config.window.height;

        // Effective GLOBAL top-left of the sprite (position + float wobble).
        let gx = self.current_x + self.float_x;
        let gy = self.current_y + self.float_y;

        // Snapshot the current sprite frame once (avoids borrowing self across
        // the per-surface mutable loop).
        let frame: Option<Vec<u8>> = self.sprites.current_frame().map(|p| p.to_vec());
        // Render the speech bubble once; its (dx, dy) are relative to the
        // sprite's top-left corner.
        let bubble: Option<(Vec<u8>, i32, i32, u32, u32)> =
            self.speech.as_ref().map(|b| b.render(sprite_w));

        let visible = self.visible;

        for surf in self.surfaces.iter_mut() {
            if !surf.configured {
                continue; // must wait for the first configure before attaching
            }

            // --- Hidden: detach any buffer and commit a blank surface. ---
            if !visible {
                let s = surf.layer.wl_surface();
                s.attach(None, 0, 0);
                s.commit();
                surf.prev_rect = (0, 0, 0, 0);
                continue;
            }

            let width = surf.width;
            let height = surf.height;
            let lx = (gx - surf.origin_x as f64).round() as i32;
            let ly = (gy - surf.origin_y as f64).round() as i32;
            let on_output = lx < width as i32
                && ly < height as i32
                && lx + sprite_w as i32 > 0
                && ly + sprite_h as i32 > 0;
            if !on_output && surf.prev_rect.2 == 0 {
                continue;
            }
            let stride = width as i32 * 4;

            let (buffer, canvas) = match surf.pool.create_buffer(
                width as i32,
                height as i32,
                stride,
                wl_shm::Format::Argb8888,
            ) {
                Ok(pair) => pair,
                Err(e) => {
                    warn!("Failed to create wl_shm buffer for output: {}", e);
                    continue;
                }
            };

            // Output-local sprite top-left.
            let local_x = (gx - surf.origin_x as f64).round() as i32;
            let local_y = (gy - surf.origin_y as f64).round() as i32;

            // Does the sprite box intersect this output at all?
            let intersects = local_x < width as i32
                && local_y < height as i32
                && local_x + sprite_w as i32 > 0
                && local_y + sprite_h as i32 > 0;

            // The rect we will draw into this frame (sprite, possibly union
            // with the bubble). Used both for damage and for next-frame clear.
            let mut draw_rect = (0i32, 0i32, 0u32, 0u32);

            surf.renderer.clear();
            if intersects {
                if let Some(ref pixels) = frame {
                    surf.renderer
                        .blit_sprite_at(pixels, sprite_w, sprite_h, local_x, local_y);
                }
                draw_rect = (local_x, local_y, sprite_w, sprite_h);

                if let Some((ref bpx, bdx, bdy, bw, bh)) = bubble {
                    let bx = (local_x + bdx).clamp(0, width.saturating_sub(bw) as i32);
                    let above = local_y + bdy;
                    let by = if above >= 8 {
                        above
                    } else {
                        local_y + sprite_h as i32 + 8
                    }
                    .clamp(0, height.saturating_sub(bh) as i32);
                    surf.renderer.blit_overlay(bpx, bx, by, bw, bh);
                    draw_rect = union_rect(draw_rect, (bx, by, bw, bh));
                }
            }

            // Convert RGBA -> premultiplied ARGB8888 (what wl_shm expects).
            // Only the rows we may have touched need converting, but the buffer
            // came back as whatever the previous slot held, so convert the
            // whole thing (it is cleared to transparent by the renderer first).
            let rgba = surf.renderer.pixels();
            canvas.fill(0);
            let x0 = draw_rect.0.max(0) as u32;
            let y0 = draw_rect.1.max(0) as u32;
            let x1 = (draw_rect.0 + draw_rect.2 as i32).clamp(0, width as i32) as u32;
            let y1 = (draw_rect.1 + draw_rect.3 as i32).clamp(0, height as i32) as u32;
            for y in y0..y1 {
                for x in x0..x1 {
                    let o = ((y * width + x) * 4) as usize;
                    let a = rgba[o + 3] as u32;
                    canvas[o] = (rgba[o + 2] as u32 * a / 255) as u8;
                    canvas[o + 1] = (rgba[o + 1] as u32 * a / 255) as u8;
                    canvas[o + 2] = (rgba[o] as u32 * a / 255) as u8;
                    canvas[o + 3] = a as u8;
                }
            }

            let surface = surf.layer.wl_surface();
            surface.attach(Some(buffer.wl_buffer()), 0, 0);

            // Damage only what changed: this frame's draw rect unioned with the
            // previous frame's rect (so the old sprite location gets cleared).
            let damage = union_rect(draw_rect, surf.prev_rect);
            if damage.2 > 0 && damage.3 > 0 {
                // Clip the damage rect to the buffer bounds (damage_buffer with
                // out-of-bounds coords is a protocol error on some compositors).
                let dx0 = damage.0.max(0);
                let dy0 = damage.1.max(0);
                let dx1 = (damage.0 + damage.2 as i32).min(width as i32);
                let dy1 = (damage.1 + damage.3 as i32).min(height as i32);
                if dx1 > dx0 && dy1 > dy0 {
                    surface.damage_buffer(dx0, dy0, dx1 - dx0, dy1 - dy0);
                }
            }
            surf.prev_rect = draw_rect;

            surface.commit();
        }
    }
}

/// Union of two `(x, y, w, h)` rectangles. A rect with `w == 0 || h == 0` is
/// treated as empty.
fn union_rect(a: (i32, i32, u32, u32), b: (i32, i32, u32, u32)) -> (i32, i32, u32, u32) {
    let a_empty = a.2 == 0 || a.3 == 0;
    let b_empty = b.2 == 0 || b.3 == 0;
    if a_empty {
        return b;
    }
    if b_empty {
        return a;
    }
    let x0 = a.0.min(b.0);
    let y0 = a.1.min(b.1);
    let x1 = (a.0 + a.2 as i32).max(b.0 + b.2 as i32);
    let y1 = (a.1 + a.3 as i32).max(b.1 + b.3 as i32);
    (x0, y0, (x1 - x0) as u32, (y1 - y0) as u32)
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

    /// A new output appeared (monitor plugged in / created). Spin up a surface
    /// for it so the buddy can roam onto it.
    fn new_output(
        &mut self,
        _conn: &Connection,
        qh: &QueueHandle<Self>,
        output: wl_output::WlOutput,
    ) {
        self.create_output_surface(output, qh);
    }

    /// An output's properties changed (e.g. moved, resized, rescaled). Refresh
    /// our cached global origin so global->local translation stays correct.
    fn update_output(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        output: wl_output::WlOutput,
    ) {
        self.refresh_output_geometry(&output);
    }

    /// An output went away (monitor unplugged). Drop its surface; SCTK/Wayland
    /// tears down the underlying objects. Dropping the `Region` destroys it too.
    fn output_destroyed(
        &mut self,
        _conn: &Connection,
        _qh: &QueueHandle<Self>,
        output: wl_output::WlOutput,
    ) {
        if let Some(idx) = self.surfaces.iter().position(|s| s.output == output) {
            info!("Output destroyed; removing its overlay surface");
            self.surfaces.remove(idx);
        }
    }
}

impl LayerShellHandler for BuddyApp {
    /// Called when the compositor wants to close our surface.
    fn closed(&mut self, _conn: &Connection, _qh: &QueueHandle<Self>, _layer: &LayerSurface) {
        info!("Layer surface closed by compositor");
        self.exit = true;
    }

    /// Called when the compositor configures one of our surfaces (initial and
    /// on resize). We size that surface's buffer to the full output and, on the
    /// very first configure of the very first surface, choose the buddy's
    /// initial resting position from the config anchor/margins.
    fn configure(
        &mut self,
        _conn: &Connection,
        qh: &QueueHandle<Self>,
        layer: &LayerSurface,
        configure: LayerSurfaceConfigure,
        _serial: u32,
    ) {
        // Find which output surface this configure is for.
        let idx = match self.surfaces.iter().position(|s| &s.layer == layer) {
            Some(i) => i,
            None => return,
        };

        // Decide this surface's pixel size. With anchor on all 4 edges and
        // size 0x0, the compositor reports the full output size in new_size;
        // fall back to the surface's current size if it sends 0.
        let new_w = if configure.new_size.0 != 0 {
            configure.new_size.0
        } else {
            self.surfaces[idx].width.max(1)
        };
        let new_h = if configure.new_size.1 != 0 {
            configure.new_size.1
        } else {
            self.surfaces[idx].height.max(1)
        };

        let first_surface_configure = !self.surfaces[idx].configured;

        {
            let surf = &mut self.surfaces[idx];
            surf.width = new_w;
            surf.height = new_h;
            // Resize the renderer to match the output and grow the pool so a
            // full-output buffer fits.
            surf.renderer.resize(new_w, new_h);
            let needed = (new_w as usize) * (new_h as usize) * 4;
            if surf.pool.len() < needed {
                let _ = surf.pool.resize(needed);
            }
            surf.configured = true;
            surf.prev_rect = (0, 0, 0, 0);
            info!(
                "Configure surface @origin=({},{}) -> {}x{}",
                surf.origin_x, surf.origin_y, new_w, new_h
            );
        }

        // On the very first configure across all surfaces, pick the initial
        // GLOBAL resting position from the config anchor + margins, measured
        // relative to THIS output's geometry. After the brain sends its first
        // `move`, this is overridden anyway.
        if first_surface_configure && !self.have_target {
            let surf = &self.surfaces[idx];
            let mx = self.config.window.margin_x;
            let my = self.config.window.margin_y;
            let sw = self.config.window.width as i32;
            let sh = self.config.window.height as i32;
            let ow = new_w as i32;
            let oh = new_h as i32;
            // Local resting position inside this output for the chosen corner.
            let (lx, ly) = match self.config.window.anchor.as_str() {
                "bottom-right" => (ow - sw - mx, oh - sh - my),
                "bottom-left" => (mx, oh - sh - my),
                "top-right" => (ow - sw - mx, my),
                "top-left" => (mx, my),
                _ => (ow - sw - mx, oh - sh - my),
            };
            // Convert to GLOBAL coordinates using this output's origin.
            self.current_x = (surf.origin_x + lx) as f64;
            self.current_y = (surf.origin_y + ly) as f64;
            self.target_x = self.current_x;
            self.target_y = self.current_y;
            info!(
                "Initial resting global position: ({}, {})",
                self.current_x, self.current_y
            );
        }

        // Draw (this also re-commits). draw() iterates all configured surfaces.
        self.draw(qh);
    }
}

impl SeatHandler for BuddyApp {
    fn seat_state(&mut self) -> &mut SeatState {
        &mut self.seat_state
    }

    fn new_seat(&mut self, _conn: &Connection, _qh: &QueueHandle<Self>, _seat: wl_seat::WlSeat) {}

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

    fn remove_seat(&mut self, _conn: &Connection, _qh: &QueueHandle<Self>, _seat: wl_seat::WlSeat) {
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
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--preview") {
        let path = args.get(2).expect("Usage: --preview OUTPUT.png");
        preview::render(std::path::Path::new(path), &config).expect("render preview");
        return;
    }
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

    // Parse layer from config (overlay/top/bottom/background).
    let layer = match config.window.layer.as_str() {
        "overlay" => Layer::Overlay,
        "top" => Layer::Top,
        "bottom" => Layer::Bottom,
        "background" => Layer::Background,
        _ => Layer::Overlay,
    };

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

    // --- Build app state ---
    let now = Instant::now();
    let mut app = BuddyApp {
        registry_state,
        seat_state,
        output_state,
        compositor_state,
        shm,
        layer_shell,
        layer,
        surfaces: Vec::new(),
        config,
        sprites,
        speech: None,
        current_state: "idle".to_string(),
        revert_state: None,
        visible: true,
        current_x: 0.0,
        current_y: 0.0,
        target_x: 0.0,
        target_y: 0.0,
        have_target: false,
        float_x: 0.0,
        float_y: 0.0,
        needs_redraw: true,
        exit: false,
        last_frame: now,
        start_time: now,
    };

    // --- Create one full-output overlay surface per existing output ---
    // Outputs that appear/disappear later are handled dynamically by the
    // OutputHandler (new_output / output_destroyed).
    let outputs: Vec<wl_output::WlOutput> = app.output_state.outputs().collect();
    if outputs.is_empty() {
        warn!("No outputs found at startup; surfaces will be created when outputs appear");
    }
    for output in outputs {
        app.create_output_surface(output, &qh);
    }

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
    //
    // Note: while moving/floating/animating/speaking we ALSO get woken by
    // per-surface frame callbacks (requested in draw()). This timer is the
    // fallback heartbeat: it keeps things ticking and, crucially, backs off to
    // a slow interval when fully idle so idle CPU stays ~0.
    let timer = Timer::from_duration(Duration::from_millis(100));
    let qh_for_timer = qh.clone();
    loop_handle
        .insert_source(timer, move |_, _, state: &mut BuddyApp| {
            let dirty = state.tick();

            // Determine the next tick interval based on activity.
            let interval = if state.is_fully_idle() {
                // Nothing in motion: sleep long. Frame callbacks will not fire
                // either (draw() requests none), so we're effectively asleep.
                Duration::from_millis(500)
            } else if state.speech.is_some() || state.sprites.is_transitioning() {
                let fps = 30;
                Duration::from_secs_f64(1.0 / fps as f64)
            } else {
                // Moving / floating: run near the idle fps.
                let fps = 30;
                Duration::from_secs_f64(1.0 / fps as f64)
            };

            let any_configured = state.surfaces.iter().any(|s| s.configured);
            if (dirty || state.needs_redraw) && any_configured {
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
