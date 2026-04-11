from openai import AsyncOpenAI

from src.config import settings

_client = AsyncOpenAI(api_key=settings.openai_api_key)


async def embed_text(text: str) -> list[float]:
    """Embed a single text string."""
    text = text.replace("\n", " ").strip()[:8000]  # token safety
    response = await _client.embeddings.create(
        model=settings.embedding_model,
        input=text,
        dimensions=settings.embedding_dimensions,
    )
    return response.data[0].embedding


async def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts in one API call (max 2048 inputs)."""
    if not texts:
        return []
    cleaned = [(t or "").replace("\n", " ").strip()[:8000] for t in texts]
    response = await _client.embeddings.create(
        model=settings.embedding_model,
        input=cleaned,
        dimensions=settings.embedding_dimensions,
    )
    return [r.embedding for r in response.data]
