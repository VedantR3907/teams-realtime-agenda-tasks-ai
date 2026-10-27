from typing import Dict, Callable, Optional
from loguru import logger
from app.ProgressAnalyzer.STT import DeepgramTranscriber
from app.config import settings


class ParticipantManager:
    """
    Manages participant transcribers and display names.
    
    This class handles:
    - Creating and managing Deepgram transcribers per participant
    - Participant name mapping and display name resolution
    - Transcriber lifecycle management (creation, activation checks, cleanup)
    - Audio data routing to appropriate transcribers
    - Participant status tracking
    """
    
    def __init__(self):
        # Store transcribers per participant (using email as unique identifier)
        self.participant_transcribers: Dict[str, DeepgramTranscriber] = {}
        # Store participant information (email -> display_name mapping)
        self.participant_names: Dict[str, str] = {}

    def update_participant_name(self, email: str, display_name: str) -> bool:
        """Update a participant's display name and refresh their transcriber callback if needed"""
        if not email or not display_name:
            return False
            
        old_name = self.participant_names.get(email)
        if old_name != display_name:
            self.participant_names[email] = display_name
            logger.debug(f"Updated participant: {display_name} (Email: {email})")
            return True
        return False

    def get_or_create_transcriber(self, email: str, transcript_callback: Callable[[str, bool, float, float], None]) -> Optional[DeepgramTranscriber]:
        """Get existing transcriber or create a new one for the participant"""
        if not email:
            logger.warning("No email provided for transcriber creation")
            return None

        # Get existing transcriber or check if we need to create/recreate one
        transcriber = self.participant_transcribers.get(email)
        # Avoid recreating while a transcriber is still starting; let it finish first
        if transcriber is not None and hasattr(transcriber, 'is_starting') and transcriber.is_starting():
            logger.debug(f"Transcriber for {email} is starting; skipping recreate")
            return transcriber
        needs_create = transcriber is None or not transcriber.is_active()
        if needs_create:
            # Use Keyterm Prompting for Nova-3 (better recognition)
            # keyterms = [ ]
            
            # logger.info(f"{'Recreating' if transcriber else 'Creating'} transcriber for {email} with keyterms: {keyterms}")
            
            try:
                if not settings.DEEPGRAM_API_KEY:
                    logger.error("DEEPGRAM_API_KEY is not configured")
                    return None
                # If there is an existing but inactive transcriber, stop it before recreating
                was_recreate = transcriber is not None
                if was_recreate:
                    try:
                        transcriber.stop()
                    except Exception as stop_err:
                        logger.debug(f"Error stopping inactive transcriber for {email}: {stop_err}")

                transcriber = DeepgramTranscriber(
                    api_key=settings.DEEPGRAM_API_KEY,
                    user_identifier=email,
                    on_transcript_callback=transcript_callback,
                    # keyterms=keyterms
                )
                self.participant_transcribers[email] = transcriber
                logger.info(f"Successfully {'recreated' if was_recreate else 'created'} transcriber for {email}")
                return transcriber
            except Exception as e:
                logger.error(f"Failed to {'recreate' if was_recreate else 'create'} transcriber for {email}: {e}")
                return None
        
        return transcriber

    def send_audio_to_participant(self, email: str, pcm_data: bytes, speak_start: float, speak_end: float) -> bool:
        """Send audio data to a participant's transcriber"""
        transcriber = self.participant_transcribers.get(email)
        if transcriber:
            transcriber.send_audio(pcm_data, speak_start, speak_end)
            return True
        else:
            logger.warning(f"No transcriber found for participant {email}")
            return False

    def get_participant_display_name(self, email: str) -> str:
        """Get participant's display name, falling back to email if not available"""
        return self.participant_names.get(email, email)

    def add_participants_from_list(self, participants: list) -> None:
        """Add participants from a list of participant dictionaries"""
        for participant in participants:
            email = participant.get("email")
            display_name = participant.get("name", "Unknown User")
            if email:
                self.update_participant_name(email, display_name)

    def stop_all_transcribers(self):
        """Stop all active transcribers and clear the collection"""
        logger.info(f"Stopping {len(self.participant_transcribers)} transcribers")
        for transcriber in self.participant_transcribers.values():
            try:
                transcriber.stop()
            except Exception as e:
                logger.error(f"Error stopping transcriber: {e}")
        self.participant_transcribers.clear()

    def get_participant_count(self) -> int:
        """Get the current number of participants"""
        return len(self.participant_names)

    def get_all_participants(self) -> Dict[str, str]:
        """Get all participants (email -> display_name mapping)"""
        return self.participant_names.copy()

    def get_transcriber_status(self) -> Dict[str, bool]:
        """Get status of all transcribers"""
        return {
            email: transcriber.is_active() if transcriber else False
            for email, transcriber in self.participant_transcribers.items()
        }

    def get_participant_stats(self) -> Dict[str, Dict]:
        """Get comprehensive participant statistics"""
        stats = {}
        for email, display_name in self.participant_names.items():
            transcriber = self.participant_transcribers.get(email)
            stats[email] = {
                "display_name": display_name,
                "has_transcriber": transcriber is not None,
                "transcriber_active": transcriber.is_active() if transcriber else False
            }
        return stats 