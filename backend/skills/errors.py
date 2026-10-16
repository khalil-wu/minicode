"""Skill catalog admission failures owned by the receiving turn."""


class RequiredSkillUnavailableError(ValueError):
    """A captured environment's required Skill is absent from its catalog."""
