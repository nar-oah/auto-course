from pydantic import BaseModel, ConfigDict, Field


class Credentials(BaseModel):
    """Credentials passed from the API to the study worker."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1)
    password: str = Field(min_length=1, repr=False)
