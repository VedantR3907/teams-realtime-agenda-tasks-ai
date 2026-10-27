"""
Processors package for the meeting agenda & tasks AI system.

Contains specialized processors for different aspects of meeting analysis:
- LLMProcessor: Agenda state change detection
- ProgressLLMProcessor: Progress item extraction (Decisions, Agreements, Issues)
# - NameCorrectionProcessor: Assignee name standardization (DISABLED: Now integrated into DAI prompt)
"""

from .llm_processor import LLMProcessor
from .progress_llm_processor import ProgressLLMProcessor
# from .name_correction_processor import NameCorrectionProcessor  # DISABLED: Name correction now integrated into DAI prompt

__all__ = [
    "LLMProcessor",
    "ProgressLLMProcessor",
    # "NameCorrectionProcessor"  # DISABLED: Name correction now integrated into DAI prompt
] 