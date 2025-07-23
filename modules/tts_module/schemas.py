from pydantic import BaseModel, Field
from typing import Optional

class TTSInput(BaseModel):
    text: str = Field(..., description="Text content to synthesize")
    mood: Optional[str] = Field(None, description="Emotional mood for the voice")
    save: bool = Field(False, description="Whether to save the output audio file")
    force_chunking: Optional[bool] = Field(False, description="Force chunking even for short text")

class TTSOutput(BaseModel):
    status: str = Field(..., description="Status of the TTS process")
    output_path: Optional[str] = Field(None, description="Path to the output audio file if saved")
    message: str = Field(..., description="Status message or error description")
    is_chunked: bool = Field(False, description="Whether the text was processed as chunks")
    chunk_count: Optional[int] = Field(None, description="Number of chunks if chunking was used")

class TTSQueueStatus(BaseModel):  # Currently not used
    is_playing: bool = Field(..., description="Whether TTS is currently playing")
    queue_length: int = Field(..., description="Number of chunks in the queue")
