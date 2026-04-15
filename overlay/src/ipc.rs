//! IPC Server
//!
//! Listens on a Unix domain socket for JSON commands from the brain component.
//! Uses non-blocking I/O so we can poll for commands without blocking the
//! render loop.
//!
//! Protocol: each command is a single line of JSON terminated by '\n'.
//!
//! Supported commands:
//! - `{"cmd": "set_state", "state": "happy", "duration": 5.0}`
//! - `{"cmd": "say", "text": "Hello!", "state": "talking"}`
//! - `{"cmd": "move", "x": 100, "y": 100}`
//! - `{"cmd": "visibility", "visible": false}`
//! - `{"cmd": "quit"}`

use std::io::{self, BufRead, BufReader, ErrorKind};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};

use serde::Deserialize;

/// Parsed IPC command.
#[derive(Debug)]
pub enum IpcCommand {
    SetState { state: String, duration: f64 },
    Say { text: String, state: String },
    Move { x: i32, y: i32 },
    Visibility { visible: bool },
    Quit,
}

/// Raw JSON message for deserialization.
#[derive(Deserialize)]
struct RawCommand {
    cmd: String,
    state: Option<String>,
    duration: Option<f64>,
    text: Option<String>,
    x: Option<i32>,
    y: Option<i32>,
    visible: Option<bool>,
}

impl RawCommand {
    fn into_command(self) -> Option<IpcCommand> {
        match self.cmd.as_str() {
            "set_state" => {
                Some(IpcCommand::SetState {
                    state: self.state.unwrap_or_else(|| "idle".to_string()),
                    duration: self.duration.unwrap_or(0.0),
                })
            }
            "say" => {
                Some(IpcCommand::Say {
                    text: self.text.unwrap_or_default(),
                    state: self.state.unwrap_or_else(|| "talking".to_string()),
                })
            }
            "move" => {
                Some(IpcCommand::Move {
                    x: self.x.unwrap_or(0),
                    y: self.y.unwrap_or(0),
                })
            }
            "visibility" => {
                Some(IpcCommand::Visibility {
                    visible: self.visible.unwrap_or(true),
                })
            }
            "quit" => Some(IpcCommand::Quit),
            _ => {
                log::warn!("Unknown IPC command: {}", self.cmd);
                None
            }
        }
    }
}

/// Non-blocking IPC server on a Unix domain socket.
pub struct IpcServer {
    listener: UnixListener,
    socket_path: PathBuf,
    /// Currently connected client, if any.
    client: Option<BufReader<UnixStream>>,
    /// Buffer for partial line reads.
    line_buf: String,
}

impl IpcServer {
    /// Create a new IPC server, binding to the given socket path.
    ///
    /// Removes any stale socket file first.
    pub fn new(path: &str) -> io::Result<Self> {
        let socket_path = PathBuf::from(path);

        // Remove stale socket if it exists
        if socket_path.exists() {
            std::fs::remove_file(&socket_path)?;
        }

        let listener = UnixListener::bind(&socket_path)?;

        // Set non-blocking so poll() doesn't hang
        listener.set_nonblocking(true)?;

        Ok(Self {
            listener,
            socket_path,
            client: None,
            line_buf: String::new(),
        })
    }

    /// Poll for the next available command. Non-blocking.
    ///
    /// Returns:
    /// - `Ok(Some(cmd))` if a command was received
    /// - `Ok(None)` if no data available right now
    /// - `Err(e)` on I/O error
    pub fn poll(&mut self) -> io::Result<Option<IpcCommand>> {
        // Accept new connections (non-blocking)
        match self.listener.accept() {
            Ok((stream, _addr)) => {
                log::info!("IPC client connected");
                stream.set_nonblocking(true)?;
                self.client = Some(BufReader::new(stream));
            }
            Err(ref e) if e.kind() == ErrorKind::WouldBlock => {
                // No new connection, that's fine
            }
            Err(e) => return Err(e),
        }

        // Read from current client
        if let Some(ref mut reader) = self.client {
            self.line_buf.clear();
            match reader.read_line(&mut self.line_buf) {
                Ok(0) => {
                    // Client disconnected
                    log::info!("IPC client disconnected");
                    self.client = None;
                    return Ok(None);
                }
                Ok(_) => {
                    let line = self.line_buf.trim();
                    if line.is_empty() {
                        return Ok(None);
                    }

                    match serde_json::from_str::<RawCommand>(line) {
                        Ok(raw) => return Ok(raw.into_command()),
                        Err(e) => {
                            log::warn!("Invalid IPC JSON: {} (input: '{}')", e, line);
                            return Ok(None);
                        }
                    }
                }
                Err(ref e) if e.kind() == ErrorKind::WouldBlock => {
                    return Ok(None);
                }
                Err(e) => {
                    log::warn!("IPC read error: {}", e);
                    self.client = None;
                    return Err(e);
                }
            }
        }

        Ok(None)
    }
}

impl Drop for IpcServer {
    fn drop(&mut self) {
        // Clean up the socket file on exit
        if self.socket_path.exists() {
            let _ = std::fs::remove_file(&self.socket_path);
        }
    }
}
