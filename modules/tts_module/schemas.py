from pydantic import BaseModel, Field
from typing import Optional, List

class TTSInput(BaseModel):
    text: str = Field(..., description="Text content to synthesize")
    mood: Optional[str] = Field(None, description="Emotional mood for the voice (deprecated, for backward compatibility)")
    save: bool = Field(False, description="Whether to save the output audio file")
    force_chunking: Optional[bool] = Field(False, description="Force chunking even for short text")
    character: Optional[str] = Field(None, description="Character to use for TTS (None = use default)")
    emotion_vector: Optional[List[float]] = Field(None, description="8D emotion vector [happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]. None = derive from Status Manager")

class TTSOutput(BaseModel):
    status: str = Field(..., description="Status of the TTS process")
    output_path: Optional[str] = Field(None, description="Path to the output audio file if saved")
    message: str = Field(..., description="Status message or error description")
    is_chunked: bool = Field(False, description="Whether the text was processed as chunks")
    chunk_count: int = Field(0, description="Number of chunks if chunking was used")
    audio_duration: Optional[float] = Field(default=None, description="Audio duration in seconds")

class TTSQueueStatus(BaseModel):  # Currently not used
    is_playing: bool = Field(..., description="Whether TTS is currently playing")
    queue_length: int = Field(..., description="Number of chunks in the queue")
