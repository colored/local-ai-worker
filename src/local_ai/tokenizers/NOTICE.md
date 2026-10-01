# Offline tokenizer data

These unmodified tokenizer files are not model weights. They are loaded locally;
the application never downloads tokenizer or model data at runtime. Their SHA-256
fingerprints and source repositories are recorded in manifest.json.

## Gemma 3

Original owner: Google DeepMind. The tokenizer is from the public Unsloth mirror
of Google's Gemma 3 12B tokenizer; the same tokenizer serves the default 27B profile.

- Source: https://huggingface.co/unsloth/gemma-3-12b-it
- Original model: https://huggingface.co/google/gemma-3-12b-it
- Terms: https://ai.google.dev/gemma/terms
- Usage restrictions: https://ai.google.dev/gemma/prohibited_use_policy

Gemma is provided under and subject to the Gemma Terms of Use found at
https://ai.google.dev/gemma/terms. This application's MIT license does not replace
those terms. The upstream Terms page is included in gemma3.terms.html.

## NVIDIA Nemotron 3 Nano

Owner: NVIDIA. Source:
https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16

Subject to NVIDIA's Nemotron Open Model License:
https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-nemotron-open-model-license/
The upstream license page is included in nemotron3.terms.html.
