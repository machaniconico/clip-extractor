# Experimental local LLM compliance and distribution record

- Last checked: 2026-08-04
- Status: `LOCAL-LLM-READINESS: EXPERIMENTAL`
- Distribution default: `DISABLED`
- Feature flag: `CLIP_EXTRACTOR_ENABLE_LOCAL_LLM=1`
- Default endpoint: `http://127.0.0.1:1234/v1`
- Protocol: OpenAI-compatible Chat Completions with JSON Schema

## Primary sources

- OpenAI-compatible endpoints: https://lmstudio.ai/docs/developer/openai-compat
- Chat Completions: https://lmstudio.ai/docs/developer/openai-compat/chat-completions
- Structured output: https://lmstudio.ai/docs/developer/openai-compat/structured-output
- Model listing and capabilities: https://lmstudio.ai/docs/developer/rest/list
- Local server settings: https://lmstudio.ai/docs/developer/core/server/settings
- Authentication: https://lmstudio.ai/docs/developer/core/authentication

## Current scope

- The regular sales experience does not show the local provider. It appears only after explicit environment opt-in.
- Clip Extractor sends the Whisper transcript, highlight instructions, and JSON Schema to the selected loopback server.
- Source video, audio, images, and extracted frames are not sent by this implementation.
- The app does not download, load, update, or redistribute LM Studio, a local runtime, GGUF files, model weights, or vision projectors.
- The customer must choose and operate their own compatible model. Model quality, VRAM needs, context length, runtime support, and license obligations are not guaranteed.

## Security and privacy boundaries

- Only `http` endpoints on `localhost`, `127.0.0.1`, or `::1` with the exact `/v1` path are accepted.
- LAN addresses, `0.0.0.0`, public hosts, HTTPS endpoints, userinfo, query strings, fragments, and other paths are rejected before the OpenAI client is created.
- The OpenAI client receives the non-secret placeholder key `lm-studio`; saved Gemini/OpenAI credentials are never forwarded to the local provider.
- The dedicated HTTP client ignores proxy environment variables and refuses redirects, so a proxy or redirect cannot forward transcript requests away from the validated loopback endpoint.
- Calls use a 3-second connect bound, 600-second read bound, and `max_retries=0`. Invalid structured output fails closed without a second generation or cloud fallback.
- The current feature does not expose a remote OpenAI-compatible endpoint. Adding one would require a separate product, privacy, authentication, and SSRF review.

## Distribution decision

The local adapter may remain in the sales ZIP because it is dormant, does not bundle third-party models or runtimes, and does not change the default Gemini workflow. It must continue to be described as experimental until the release gates below are completed.

Do not advertise a specific local model as commercially supported until its exact upstream model, quantization, runtime, license, required notices, hardware matrix, and representative-video quality have been recorded and verified.

## Gates before general availability

- Benchmark representative videos on supported 8 GB, 12 GB, 16 GB, 24 GB, and 32 GB GPU tiers.
- Define minimum acceptable highlight quality and maximum processing time relative to the supported cloud default.
- Add model discovery through LM Studio's native model-list endpoint and show only loaded LLMs.
- Record the exact license and notices for every recommended model/runtime combination.
- Decide whether authenticated local servers are supported without persisting their token in browser state or plaintext settings.
- Add visual-model capability detection before implementing candidate-frame or video analysis.
- Add end-user setup and troubleshooting only after the supported hardware/model matrix is stable.
