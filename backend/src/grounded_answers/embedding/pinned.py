"""
The embedding model this code uses, pinned (ADR 0021, ADR 0022 decision D7).

The revision is a Hugging Face commit. Every file is checked by SHA-256 and size, so a changed or corrupted
file fails loudly. Changing the model is a deliberate migration (ADR 0021, decision 6): change these values
and the migration that stores the new model, never silently.
"""

from types import MappingProxyType
from typing import Final

MODEL_NAME: Final = "intfloat/multilingual-e5-small"
MODEL_REVISION: Final = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
MODEL_LICENCE: Final = "MIT"
# The model file, relative to the model directory. fastembed reads this path.
MODEL_FILE: Final = "onnx/model.onnx"

# The e5 model card requires these prefixes on every input (ADR 0021).
PASSAGE_PREFIX: Final = "passage: "
QUERY_PREFIX: Final = "query: "

# Inputs per inference call. Measured on the demo set (ADR 0021).
BATCH_SIZE: Final = 8

# Relative path -> (SHA-256, size in bytes). Taken from the pinned revision.
PINNED_FILES: Final[MappingProxyType[str, tuple[str, int]]] = MappingProxyType(
    {
        "onnx/model.onnx": (
            "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
            470268510,
        ),
        "tokenizer.json": (
            "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
            17082730,
        ),
        "config.json": (
            "69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959",
            655,
        ),
        "special_tokens_map.json": (
            "d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7",
            167,
        ),
        "tokenizer_config.json": (
            "a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b",
            443,
        ),
    }
)
