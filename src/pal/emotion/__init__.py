"""Plugin-owned emotion broadcast and renderer contribution interface."""
from pal.emotion.runtime import EmotionReceiver, EmotionRegistry, build_emotion_handle

__all__ = ["EmotionReceiver", "EmotionRegistry", "build_emotion_handle"]
