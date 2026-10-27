from typing import Optional
import queue
import threading
import time
import asyncio
import json
import os
from loguru import logger
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
import struct
import audioop
from app.config import settings

class TranscriptCollector:
    """Collects transcript parts until a final transcript is received."""
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.parts = []  # collect all is_final parts

    def add_part(self, part: str):
        with self._lock:
            self.parts.append(part)  # append each is_final part

    def get_full_transcript(self):
        with self._lock:
            return " ".join(self.parts) if self.parts else ""

class DeepgramTranscriber:
    """
    Simple Deepgram transcriber that writes transcripts to file in real-time,
    and sends KeepAlive pings to avoid timeouts.
    """
    def __init__(
        self,
        api_key: str,
        user_identifier: str,
        transcript_path: Optional[str] = None,
        model: Optional[str] = None,
        language: Optional[str] = None,
        sample_rate: int = 16000,
        channels: int = 1,
        interim: bool = True,
        # Lower values improve latency; adjustable via parameters
        utterance_end_ms: int = 1000,
        vad_events: bool = True,
        endpointing: int = 100,
        on_transcript_callback=None,
        # keyterms: Optional[list] = None,
    ):
        # Use settings defaults if not provided
        if transcript_path is None:
            transcript_path = settings.STT_TRANSCRIPT_PATH
        if model is None:
            model = settings.STT_MODEL
        if language is None:
            language = settings.STT_LANGUAGE
            
        # Create transcript folder if it doesn't exist
        if settings.STT_WRITE_FILES:
            os.makedirs(transcript_path, exist_ok=True)
        
        # Generate safe filename from user identifier (replace invalid characters)
        safe_identifier = "".join(c for c in user_identifier if c.isalnum() or c in ('-', '_', '.', '@')).replace('@', '_at_')
        
        # Transcript file paths
        self.transcript_txt_path = os.path.join(transcript_path, f"{safe_identifier}_transcript.txt")
        self.transcript_json_path = os.path.join(transcript_path, f"{safe_identifier}_transcript.json")
        
        # Initialize text transcript file
        if settings.STT_WRITE_FILES:
            open(self.transcript_txt_path, "w", encoding="utf-8").close()
        
        # Initialize JSON transcript file
        if settings.STT_WRITE_FILES:
            with open(self.transcript_json_path, "w", encoding="utf-8") as f:
                f.write("[\n")  # Start JSON array
            
        self._lock = threading.Lock()
        
        # Create Deepgram client
        logger.info(f"Creating DeepgramClient with API key: {api_key[:5]}...")
        self.dg = DeepgramClient(api_key)
        
        # remember sample rate so we can build silence
        self._sample_rate = sample_rate
        
        # Build LiveOptions with validated parameters
        opts_dict = {
            "model": model,
            "language": language,
            "smart_format": True,
            "encoding": "linear16",
            "sample_rate": sample_rate,
            "channels": channels,
            "interim_results": interim,
        }
        
        # Add optional parameters only if they're needed
        if utterance_end_ms > 0 and interim:
            # utterance_end_ms requires interim_results to be true
            opts_dict["utterance_end_ms"] = str(utterance_end_ms)
            
        if vad_events:
            opts_dict["vad_events"] = vad_events
            
        if endpointing > 0:
            opts_dict["endpointing"] = endpointing
        
        # Add keyterms if provided (for Nova-3)
        # if keyterms and len(keyterms) > 0:
        #     # Use the first keyterm in the list
        #     first_keyterm = keyterms[0]
        #     logger.info(f"Adding primary keyterm to LiveOptions: {first_keyterm}")
        #     # For Nova-3, the parameter is 'keyterm' (singular) not 'keywords'
        #     opts_dict["keyterm"] = first_keyterm
        
        logger.info(f"Creating LiveOptions with: {opts_dict}")
        try:
            self.opts = LiveOptions(**opts_dict)
            logger.info("LiveOptions created successfully")
        except Exception as e:
            logger.error(f"Failed to create LiveOptions: {e}")
            raise

        # Callbacks for transcripts
        self.on_transcript_callback = on_transcript_callback
        
        # Track the last time we received a final transcript
        self._last_final_time = time.time()
        
        # Health monitoring
        self._last_transcript_time = time.time()
        self._connection_healthy = True
        self._consecutive_errors = 0
        self._max_consecutive_errors = 3

        # Connection management timing
        self._last_audio_time = time.time()        # last real PCM from this speaker
        self._last_keepalive_time = 0.0             # last KA frame we sent
        self._idle_shutdown_timer = 0.0             # when we plan to call CloseStream
        self._silence_since = None                  # when real silence started (prevents phantom buffer issues)
        self._utterance_complete_time = None        # when last utterance was completed
        self._has_received_user_audio = False       # Track if we've received any user audio
        self._finalize_sent = False                 # Track if we've sent finalize for current gap
        self._close_sent = False                    # Track if we've sent CloseStream
        self._stop_connection_manager = False       # Control flag for connection manager
        self._connection_task: Optional[asyncio.Task] = None

        # Startup/retry state
        self._connection_started = False           # True only after successful conn.start
        self._max_start_retries = 3               # retry attempts for initial start
        self._start_retry_backoff_sec = 1.0       # base backoff in seconds
        self._needs_reconnect = False             # True when connection timed out and needs reconnect
        self._reconnect_lock = threading.Lock()   # Prevent concurrent reconnection attempts

        # Audio queue + thread
        self._audio_q = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        
        # Start connection manager using asyncio if possible
        try:
            loop = asyncio.get_running_loop()
            self._connection_task = loop.create_task(self._connection_manager())
        except RuntimeError:
            # Fallback to background thread with its own loop
            self._connection_thread = threading.Thread(
                target=lambda: asyncio.run(self._connection_manager()),
                daemon=True
            )
            self._connection_thread.start()
        
        # Transcript collector for accumulating is_final parts
        self.transcript_collector = TranscriptCollector()

    def _write_line(self, line: str):
        if not settings.STT_WRITE_FILES:
            return
        with self._lock:
            with open(self.transcript_txt_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def _on_transcript(self, _ws, result, **kwargs):
        try:
            # Log the entire result object for debugging
            logger.debug(f"Received transcript result: {result.to_dict()}")
            
            # Extract transcript details
            text = result.channel.alternatives[0].transcript.strip()
            speech_final = getattr(result, "speech_final", False)
            is_final = getattr(result, "is_final", False)
            from_finalize = getattr(result, "from_finalize", False)
            
            # Update health monitoring
            self._last_transcript_time = time.time()
            # Reset consecutive errors on successful transcript
            self._consecutive_errors = 0
            
            # Log detailed info about the transcript type
            logger.debug(f"Transcript type: speech_final={speech_final}, is_final={is_final}, from_finalize={from_finalize}")
            
            # Handle transcript collection - collect is_final parts that are not final triggers
            if is_final and text and not speech_final and not from_finalize:
                # This is a finalized part of the utterance, but not the end
                self.transcript_collector.add_part(text)
                logger.debug(f"Collected is_final part: '{text}'")
                
                # Push partial downstream immediately (final flag = False)
                if self.on_transcript_callback:
                    try:
                        self.on_transcript_callback(text, False, self._last_speak_start, self._last_speak_end)
                    except Exception as cb_err:
                        logger.error(f"Transcript callback (partial) failed: {cb_err}")
            
            # FLUSH TRIGGER: on Deepgram's built-in speech_final (end of utterance)
            if speech_final:
                # This is the end of the utterance
                if text:
                    # Add the final part too
                    self.transcript_collector.add_part(text)
                    trigger_type = "speech_final"
                    logger.debug(f"Added final part ({trigger_type}): '{text}'")
                
                # Get the complete transcript with all collected parts
                full_transcript = self.transcript_collector.get_full_transcript()
                if not full_transcript:
                    return  # Nothing to do
                
                trigger_type = "speech_final"
                logger.info(f"Complete utterance ({trigger_type}): '{full_transcript}'")
                
                # Mark when utterance was completed for KeepAlive timing
                self._utterance_complete_time = time.time()
                
                # Append to per-user transcript file
                self._write_line(f"[{time.strftime('%H:%M:%S')}] {full_transcript}")
                
                # Append JSON event to per-user JSON transcript file
                if settings.STT_WRITE_FILES:
                    with self._lock:
                        with open(self.transcript_json_path, "a", encoding="utf-8") as f:
                            json.dump(result.to_dict(), f, indent=2)
                            f.write(",\n")  # Add comma and newline for valid JSON array structure
                
                # Call the transcript callback with provided speak times if available
                if self.on_transcript_callback:
                    self.on_transcript_callback(full_transcript, True, self._last_speak_start, self._last_speak_end)
                
                # Reset the collector for the next utterance
                self.transcript_collector.reset()
                
        except Exception as e:
            logger.error(f"Error processing transcript: {e}")
            # Don't let exceptions in processing break the entire stream

    def _on_utterance_end(self, _ws, *args, **kwargs):
        # Just log the event, don't write to file
        logger.debug("Received UtteranceEnd event")

    def _on_error(self, _ws, error, **kwargs):
        """Handle Deepgram connection errors with better logging and recovery"""
        error_msg = str(error)
        logger.error(f"Deepgram error: {error}")
        
        # Track consecutive errors
        self._consecutive_errors += 1
        
        # Check for specific timeout errors
        if "timeout" in error_msg.lower() or "1011" in error_msg or "NET-0001" in error_msg:
            logger.warning("🔄 Deepgram timeout detected - will reconnect when new audio arrives")
            # Mark for reconnection (cost-effective: no silence audio needed)
            self._connection_healthy = False
            self._needs_reconnect = True
        elif "ConnectionClosed" in error_msg:
            logger.warning("🔌 Deepgram connection closed - will reconnect when new audio arrives")
            self._connection_healthy = False
            self._needs_reconnect = True
        else:
            logger.error(f"❌ Unhandled Deepgram error: {error_msg}")
        
        # If too many consecutive errors, force reconnection
        if self._consecutive_errors >= self._max_consecutive_errors:
            logger.error(f"🚨 Too many consecutive errors ({self._consecutive_errors}), forcing reconnection")
            self._stop_connection_manager = True

    def _worker(self):
        logger.info("Starting Deepgram worker thread")
        try:
            # Attempt to start connection with retries and exponential backoff
            conn = None
            for attempt in range(1, self._max_start_retries + 1):
                try:
                    # Create connection
                    conn = self.dg.listen.websocket.v("1")  # type: ignore
                    self._conn = conn  # Save connection for finalize injector

                    # Register event handlers
                    logger.debug("Registering event handlers")
                    conn.on(LiveTranscriptionEvents.Transcript, self._on_transcript)  # type: ignore
                    conn.on(LiveTranscriptionEvents.UtteranceEnd, self._on_utterance_end)  # type: ignore
                    conn.on(LiveTranscriptionEvents.Error, self._on_error)  # type: ignore

                    # Log options being used
                    logger.info(f"Starting Deepgram connection (attempt {attempt}/{self._max_start_retries}) with options: {self.opts.to_dict()}")

                    # Start streaming
                    started = conn.start(self.opts)  # type: ignore
                    if not started:
                        raise RuntimeError("Deepgram failed to start websocket")

                    # Success
                    self._connection_started = True
                    logger.info("Deepgram connection started successfully")
                    break
                except Exception as e:
                    self._connection_started = False
                    logger.error(f"Failed to start Deepgram connection (attempt {attempt}/{self._max_start_retries}): {e}")
                    # Try to close any half-open connection
                    try:
                        if conn is not None:
                            conn.finish()  # type: ignore
                    except Exception:
                        pass
                    # Backoff before retrying
                    if attempt < self._max_start_retries:
                        backoff = self._start_retry_backoff_sec * (2 ** (attempt - 1))
                        time.sleep(backoff)
                    else:
                        # Give up: mark as stopped so manager exits and PM can recreate
                        logger.error("Exhausted Deepgram start retries; marking transcriber inactive")
                        self._stop_connection_manager = True
                        return

            # Send audio until sentinel
            try:
                while True:
                    item = self._audio_q.get()
                    if item is None:
                        logger.info("Received stop signal, shutting down worker")
                        break
                        
                    pcm, speak_start, speak_end = item
                    self._last_speak_start = speak_start
                    self._last_speak_end = speak_end
                    
                    try:
                        conn.send(pcm)  # type: ignore
                        logger.debug(f"Sent {len(pcm)} bytes of audio to Deepgram")
                    except Exception as e:
                        logger.error(f"Failed to send audio to Deepgram: {e}")
                        # Continue trying to process more audio rather than breaking
                        continue
                        
            except Exception as e:
                logger.error(f"Error in main worker loop: {e}")

            # Finish up
            logger.info("Finalizing Deepgram connection")
            try:
                # we no longer rely on Finalize for flushing, but still politely close
                conn.send(json.dumps({"type": "CloseStream"}))  # type: ignore
                # Close JSON transcript array
                if settings.STT_WRITE_FILES:
                    with self._lock:
                        with open(self.transcript_json_path, "a", encoding="utf-8") as f:
                            f.write("]")
                logger.info("Successfully finalized Deepgram connection")
            except Exception as e:
                logger.error(f"Error during stream close: {e}")
            
            try:
                conn.finish()  # type: ignore
                logger.info("Deepgram connection finished")
            except Exception as e:
                logger.error(f"Error finishing Deepgram connection: {e}")
                
        except Exception as e:
            logger.error(f"Unhandled exception in Deepgram worker thread: {e}")
            if settings.STT_WRITE_FILES:
                try:
                    with self._lock:
                        with open(self.transcript_json_path, "a", encoding="utf-8") as f:
                            f.write("]")
                except Exception:
                    pass

    def _analyze_audio(self, pcm_bytes: bytes) -> dict:
        """Analyze audio chunk for basic properties."""
        try:
            # Calculate RMS (volume level)
            # PCM is 16-bit signed, so 2 bytes per sample
            if len(pcm_bytes) < 2:
                return {"rms": 0, "samples": 0, "duration_ms": 0, "has_speech": False}
            
            samples = len(pcm_bytes) // 2
            duration_ms = (samples / 16000) * 1000  # 16kHz sample rate
            
            # Calculate RMS using audioop
            rms = audioop.rms(pcm_bytes, 2)  # 2 bytes per sample
            
            # Simple speech detection - if RMS is above threshold
            has_speech = rms > 500  # Adjust threshold as needed
            
            return {
                "rms": rms,
                "samples": samples,
                "duration_ms": duration_ms,
                "has_speech": has_speech
            }
        except Exception as e:
            logger.error(f"Error analyzing audio: {e}")
            return {"rms": 0, "samples": 0, "duration_ms": 0, "has_speech": False}

    def send_audio(self, pcm_bytes: bytes, speak_start: float, speak_end: float):
        # Check if we need to reconnect after a timeout (cost-effective approach)
        if self._needs_reconnect:
            logger.info("📡 New audio arrived - triggering reconnection to Deepgram")
            self.reconnect()
            # Wait briefly for connection to start (non-blocking check)
            for _ in range(10):  # Max 1 second wait
                if self._connection_started:
                    break
                time.sleep(0.1)
            if not self._connection_started:
                logger.warning("⚠️ Reconnection in progress, audio may be delayed")
        
        # Update connection management timers - activity resumed
        self._last_audio_time = time.time()
        self._idle_shutdown_timer = self._last_audio_time + settings.STT_IDLE_TIMEOUT
        self._silence_since = None                   # we are talking again - clear silence marker
        self._utterance_complete_time = None         # reset utterance completion timing
        self._has_received_user_audio = True  # Mark that we've received user audio
        
        # Reset control flags when new user audio arrives
        self._finalize_sent = False
        self._close_sent = False
        
        # Reset finalize flag if Deepgram sent us audio again after finalize
        gap = time.time() - self._last_audio_time
        if self._finalize_sent and gap < settings.STT_FINALIZE_GAP:
            self._finalize_sent = False   # Allow next Finalize
        
        # Analyze audio before sending
        analysis = self._analyze_audio(pcm_bytes)
        
        # Log audio analysis every 5 seconds or when speech is detected
        if analysis["has_speech"] or (hasattr(self, '_last_analysis_log') and 
                                     speak_start - self._last_analysis_log > 5.0):
            self._last_analysis_log = speak_start
        elif not hasattr(self, '_last_analysis_log'):
            self._last_analysis_log = speak_start
            
        self._audio_q.put((pcm_bytes, speak_start, speak_end))

    def stop(self):
        # idempotent
        if self._stop_connection_manager:
            return

        self._stop_connection_manager = True
        self._audio_q.put(None)          # wake the worker
        # Avoid deadlock if stop() is called from within the worker thread itself
        if threading.current_thread() is not self._thread:
            self._thread.join()              # join worker-thread

        # Cancel or join connection manager
        if hasattr(self, '_connection_task') and self._connection_task:
            self._connection_task.cancel()
        elif (hasattr(self, '_connection_thread')
              and threading.current_thread() is not self._connection_thread
              and self._connection_thread.is_alive()):
            self._connection_thread.join()

    def reconnect(self):
        """
        Reconnect to Deepgram after a timeout (NET-0001).
        This is cost-effective: we only reconnect when new audio arrives,
        instead of sending silence to keep the connection alive.
        """
        with self._reconnect_lock:
            if not self._needs_reconnect:
                return  # Already reconnected or not needed
            
            logger.info("🔄 Reconnecting to Deepgram after timeout...")
            
            # Clean up old connection
            try:
                if hasattr(self, '_conn') and self._conn:
                    try:
                        self._conn.finish()
                    except Exception:
                        pass  # Connection may already be closed
            except Exception:
                pass
            
            # Reset state flags
            self._connection_started = False
            self._connection_healthy = True
            self._consecutive_errors = 0
            self._needs_reconnect = False
            self._finalize_sent = False
            self._close_sent = False
            self._stop_connection_manager = False
            
            # Clear the audio queue (old audio is stale)
            while not self._audio_q.empty():
                try:
                    self._audio_q.get_nowait()
                except queue.Empty:
                    break
            
            # Create new worker thread
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
            
            # Restart connection manager
            try:
                loop = asyncio.get_running_loop()
                self._connection_task = loop.create_task(self._connection_manager())
            except RuntimeError:
                # Fallback to background thread with its own loop
                self._connection_thread = threading.Thread(
                    target=lambda: asyncio.run(self._connection_manager()),
                    daemon=True
                )
                self._connection_thread.start()
            
            logger.info("✅ Deepgram reconnection initiated")
            
    def is_active(self) -> bool:
        """Check if the transcriber is still active (not closed due to idle timeout)"""
        return (not self._stop_connection_manager) and self._connection_started and self._thread.is_alive()

    def is_starting(self) -> bool:
        """True while worker thread is alive but websocket has not started yet."""
        return (not self._stop_connection_manager) and (not self._connection_started) and self._thread.is_alive()

    async def _connection_manager(self):
        """Runs in a task; handles KeepAlive, silence injection and CloseStream."""
        while not self._stop_connection_manager:
            now = time.time()

            # ---------------------------------------------------- KeepAlive
            # Mark start of silence period (only once)
            if self._silence_since is None and now - self._last_audio_time >= 0.3:
                # speaker just became silent → mark the start of silence
                self._silence_since = now
                self._last_keepalive_time = now   # start KA-period timing
            
            # Send KeepAlive to keep connection alive during short silence periods
            # NOTE: KeepAlive alone does NOT prevent NET-0001 timeout after 10s!
            # We intentionally let the connection timeout to save costs,
            # and reconnect when new audio arrives.
            if (
                self._silence_since is not None and
                now - self._silence_since >= 3.0 and          # 3 s real silence
                now - self._last_keepalive_time >= settings.STT_KEEPALIVE_PERIOD
            ):
                try:
                    if hasattr(self, '_conn') and self._conn:
                        self._conn.send('{"type":"KeepAlive"}')  # type: ignore
                        self._last_keepalive_time = now
                        
                        # Calculate time since utterance completion for better context
                        time_since_utterance = ""
                        if self._utterance_complete_time is not None:
                            utterance_gap = now - self._utterance_complete_time
                            time_since_utterance = f", {utterance_gap:.1f}s since utterance complete"
                        
                        logger.info(f"KeepAlive ▶ (silence gap {now - self._silence_since:.1f} s{time_since_utterance})")
                    else:
                        logger.debug("Connection not available for KeepAlive")
                except Exception as e:
                    logger.error(f"KeepAlive failed: {e}")

            # ---------------------------------------------------- Inject 270ms of silence instead of Finalize
            gap = now - self._last_audio_time
            if (
                self._has_received_user_audio and
                gap >= settings.STT_FINALIZE_GAP and
                not self._finalize_sent
                
            ):
                try:
                    if hasattr(self, '_conn') and self._conn:
                        # build 100ms of 16-bit PCM silence (smaller flush chunk → lower latency)
                        samples = int(self._sample_rate * 0.10)
                        silence = (b'\x00\x00') * samples
                        self._conn.send(silence)  # type: ignore
                        self._finalize_sent = True
                        logger.info(f"Sent 100 ms silence ▶ (gap {gap:.3f} s)")
                    else:
                        logger.warning("Connection not available for silence injection")
                except Exception as e:
                    logger.error(f"Silence injection failed: {e}")

            # ---------------------------------------------------- CloseStream
            if (
                self._has_received_user_audio and
                now >= self._idle_shutdown_timer and
                not self._close_sent
            ):
                try:
                    if hasattr(self, '_conn') and self._conn:
                        self._conn.send('{"type":"CloseStream"}')  # type: ignore
                        self._close_sent = True
                        logger.info(f"CloseStream ▶ idle {settings.STT_IDLE_TIMEOUT:.1f} s – slot freed")

                        # >>> REALLY CLOSE EVERYTHING <<<
                        try:
                            self._conn.finish()  # type: ignore  # politely close websocket
                        except Exception as e:
                            logger.debug(f"Connection finish failed (expected): {e}")
                        logger.info("Connection fully closed - all threads stopping")

                        # ---- mark the object as completely stopped ----
                        self._stop_connection_manager = True      # <- CLOSE WINDOW
                        asyncio.create_task(asyncio.to_thread(self.stop))
                        return                    # leave the manager loop
                    else:
                        logger.warning("Connection not available for CloseStream message")
                except Exception as e:
                    logger.error(f"CloseStream failed: {e}")

            await asyncio.sleep(0.2)   # 200 ms tick – more responsive keep-alive & flushing