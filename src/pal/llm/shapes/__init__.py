from pal.llm.ir import WireShape
from pal.llm.shapes.anthropic_messages import AnthropicMessagesCodec
from pal.llm.shapes.base import EncodedRequest, ShapeCodec, ShapeContext, ShapeDecodeError
from pal.llm.shapes.openai_completion import OpenAICompletionCodec
from pal.llm.shapes.openai_response import OpenAIResponseCodec

_CODECS: dict[WireShape, ShapeCodec] = {
    WireShape.OPENAI_COMPLETION: OpenAICompletionCodec(),
    WireShape.OPENAI_RESPONSE: OpenAIResponseCodec(),
    WireShape.ANTHROPIC_MESSAGES: AnthropicMessagesCodec(),
}


def codec_for_shape(shape: WireShape | str) -> ShapeCodec:
    try:
        wire_shape = shape if isinstance(shape, WireShape) else WireShape(str(shape))
    except ValueError as exc:
        raise ValueError(f"unsupported LLM wire shape: {shape}") from exc
    return _CODECS[wire_shape]

__all__ = [
    "EncodedRequest",
    "ShapeCodec",
    "ShapeContext",
    "ShapeDecodeError",
    "AnthropicMessagesCodec",
    "OpenAICompletionCodec",
    "OpenAIResponseCodec",
    "codec_for_shape",
]
