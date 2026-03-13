/**
 * NovaGuard — Nova Sonic Voice Bridge Demo
 * =========================================
 * Demonstrates Nova Sonic (InvokeModelWithBidirectionalStream) using the
 * AWS SDK for JavaScript v3, which DOES support HTTP/2 bidirectional streaming.
 *
 * This is the production voice bridge architecture for NovaGuard.
 * In production: deployed as ECS Fargate WebSocket service.
 * For the hackathon demo: run locally to prove Nova Sonic works.
 *
 * ARCHITECTURE:
 *   Deaf caller → WebSocket → ECS Fargate (this process) → Nova Sonic → transcript
 *   Transcript → POST /pipeline → Triage + Dispatch + Comms agents → Nova 2 Lite
 *   Response → WebSocket → Caller screen (text) + Dispatcher console (voice via Sonic TTS)
 *
 * Usage:
 *   node index.js                        # demo with synthetic audio
 *   node index.js --audio path/to.wav   # your WAV file
 *   node index.js --tts "text here"     # Nova Sonic TTS (text → speech)
 *   node index.js --pipeline            # full pipeline demo (Sonic → triage → dispatch → comms)
 *
 * Requirements:
 *   npm install
 *   AWS credentials with Bedrock access and nova-sonic-v1:0 model enabled
 */

import {
  BedrockRuntimeClient,
  InvokeModelWithBidirectionalStreamCommand,
} from "@aws-sdk/client-bedrock-runtime";
import { fromIni } from "@aws-sdk/credential-providers";
import * as fs from "fs";
import * as path from "path";
import * as https from "https";
import { parseArgs } from "util";

const REGION         = process.env.AWS_DEFAULT_REGION || "us-east-1";
const SONIC_MODEL_ID = "amazon.nova-sonic-v1:0";
const NOVAGUARD_API  = "https://3nf5cv59xh.execute-api.us-east-1.amazonaws.com/prod";

// ──────────────────────────────────────────────────────────────────────────────
// Parse CLI args
// ──────────────────────────────────────────────────────────────────────────────
const { values: args } = parseArgs({
  options: {
    audio:    { type: "string",  short: "a" },
    tts:      { type: "string",  short: "t" },
    pipeline: { type: "boolean", short: "p", default: false },
    help:     { type: "boolean", short: "h", default: false },
  },
  allowPositionals: true,
});

if (args.help) {
  console.log(`
NovaGuard Nova Sonic Demo
  node index.js                  # demo transcript from synthetic audio
  node index.js --audio f.wav    # transcript from WAV file
  node index.js --tts "text"     # Sonic text-to-speech
  node index.js --pipeline       # full: Sonic → 3-agent pipeline
`);
  process.exit(0);
}

const client = new BedrockRuntimeClient({
  region: REGION,
});

// ──────────────────────────────────────────────────────────────────────────────
// Synthetic demo audio (16kHz PCM WAV — 3 seconds of silence + tone)
// In production: real audio stream from WebSocket client (deaf caller's browser)
// ──────────────────────────────────────────────────────────────────────────────
function makeDemoWav(durationSecs = 3, sampleRate = 16000) {
  const numSamples = durationSecs * sampleRate;
  const pcmBuffer  = Buffer.alloc(numSamples * 2); // 16-bit samples
  for (let i = 0; i < numSamples; i++) {
    // Gentle 440Hz tone — proves non-silence to Sonic's VAD
    const sample = Math.floor(300 * Math.sin(2 * Math.PI * 440 * i / sampleRate));
    pcmBuffer.writeInt16LE(sample, i * 2);
  }
  // WAV header
  const dataLen  = pcmBuffer.length;
  const wavBuf   = Buffer.alloc(44 + dataLen);
  wavBuf.write("RIFF", 0);             wavBuf.writeUInt32LE(36 + dataLen, 4);
  wavBuf.write("WAVE", 8);             wavBuf.write("fmt ", 12);
  wavBuf.writeUInt32LE(16, 16);        // chunk size
  wavBuf.writeUInt16LE(1, 20);         // PCM
  wavBuf.writeUInt16LE(1, 22);         // mono
  wavBuf.writeUInt32LE(sampleRate, 24);
  wavBuf.writeUInt32LE(sampleRate * 2, 28); // byte rate
  wavBuf.writeUInt16LE(2, 32);         // block align
  wavBuf.writeUInt16LE(16, 34);        // bits per sample
  wavBuf.write("data", 36);            wavBuf.writeUInt32LE(dataLen, 40);
  pcmBuffer.copy(wavBuf, 44);
  return wavBuf;
}

// ──────────────────────────────────────────────────────────────────────────────
// Build Nova Sonic bidirectional stream events
// Sonic uses a multi-turn event protocol:
//   1. promptStart event — declares session
//   2. textInputChunk — inject text context as system message  
//   3. audioInputChunk(s) — streaming raw PCM audio bytes
//   4. audioInputEnd — signals audio stream complete
//   5. promptEnd — ends the session
// ──────────────────────────────────────────────────────────────────────────────
function* buildSonicEvents(audioBytes, promptName = "emergency-transcription") {
  const sessionId = `ng-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

  // 1. Session/prompt start
  yield {
    event: {
      sessionStart: {
        inferenceConfiguration: {
          maxTokens: 512,
          topP: 0.9,
          temperature: 0.2,
        },
      },
    },
  };

  yield {
    event: {
      promptStart: {
        promptName,
        textOutputConfiguration: { mediaType: "text/plain" },
        audioOutputConfiguration: {
          mediaType: "audio/lpcm",
          sampleRateHertz: 16000,
          sampleSizeBits: 16,
          channelCount: 1,
          voiceId: "matthew",
          encoding: "base64",
          audioType: "SPEECH",
        },
      },
    },
  };

  // 2. System prompt as text content block
  yield {
    event: {
      contentBlockStart: {
        promptName,
        contentBlockIndex: 0,
        role: "SYSTEM",
        type: "TEXT",
      },
    },
  };
  yield {
    event: {
      contentBlockDelta: {
        promptName,
        contentBlockIndex: 0,
        delta: {
          text: "You are a 911 emergency transcription assistant for NovaGuard. Transcribe the caller's speech exactly. If there is no speech, output: [NO SPEECH DETECTED]. Output only the transcript.",
        },
      },
    },
  };
  yield {
    event: {
      contentBlockEnd: {
        promptName,
        contentBlockIndex: 0,
      },
    },
  };

  // 3. Audio input block — stream in chunks (simulates real-time streaming)
  yield {
    event: {
      contentBlockStart: {
        promptName,
        contentBlockIndex: 1,
        role: "USER",
        type: "AUDIO",
      },
    },
  };

  const CHUNK_SIZE = 3200; // 100ms of 16kHz 16-bit audio per chunk
  for (let offset = 0; offset < audioBytes.length; offset += CHUNK_SIZE) {
    const chunk = audioBytes.slice(offset, offset + CHUNK_SIZE);
    yield {
      event: {
        contentBlockDelta: {
          promptName,
          contentBlockIndex: 1,
          delta: {
            audio: chunk.toString("base64"),
          },
        },
      },
    };
  }

  yield {
    event: {
      contentBlockEnd: {
        promptName,
        contentBlockIndex: 1,
      },
    },
  };

  // 4. End prompt
  yield { event: { promptEnd: { promptName } } };
  yield { event: { sessionEnd: {} } };
}

// ──────────────────────────────────────────────────────────────────────────────
// Nova Sonic — Speech to Text
// ──────────────────────────────────────────────────────────────────────────────
async function runNovaSonic(audioBytes) {
  console.log("\n🎙️  Nova Sonic — InvokeModelWithBidirectionalStream");
  console.log(`   Model: ${SONIC_MODEL_ID}`);
  console.log(`   Audio: ${audioBytes.length} bytes (${(audioBytes.length / 32000).toFixed(1)}s at 16kHz)`);
  console.log(`   Region: ${REGION}\n`);

  const t0 = Date.now();

  const command = new InvokeModelWithBidirectionalStreamCommand({
    modelId: SONIC_MODEL_ID,
    body: buildSonicEvents(audioBytes),
  });

  const response = await client.send(command);

  const textParts = [];
  const audioParts = [];

  // Process bidirectional stream response
  for await (const chunk of response.body) {
    if (chunk.chunk?.bytes) {
      try {
        const event = JSON.parse(Buffer.from(chunk.chunk.bytes).toString("utf8"));
        const inner = event.event || event;

        if (inner.contentBlockDelta?.delta?.text) {
          textParts.push(inner.contentBlockDelta.delta.text);
          process.stdout.write(inner.contentBlockDelta.delta.text);
        }
        if (inner.contentBlockDelta?.delta?.audio) {
          audioParts.push(inner.contentBlockDelta.delta.audio);
        }
        if (inner.metadata) {
          // Usage stats from Sonic
          const usage = inner.metadata?.usage;
          if (usage) {
            console.log(`\n\n   Tokens: ${usage.inputTokens} in / ${usage.outputTokens} out`);
          }
        }
      } catch (e) {
        // Skip non-JSON chunks (binary audio frames, etc.)
      }
    }
  }

  const transcript = textParts.join("").trim();
  const latencyMs  = Date.now() - t0;

  console.log(`\n\n✅ TRANSCRIPT: "${transcript}"`);
  console.log(`   Latency: ${latencyMs}ms | Audio chunks: ${audioParts.length}`);

  return { transcript, latencyMs, audioChunks: audioParts };
}

// ──────────────────────────────────────────────────────────────────────────────
// Nova Sonic — Text to Speech (for dispatcher briefing playback)
// ──────────────────────────────────────────────────────────────────────────────
async function runNovaSonicTTS(text) {
  console.log("\n🔊  Nova Sonic TTS — Text → Speech for dispatcher");
  console.log(`   Text: "${text}"\n`);

  const t0 = Date.now();

  // For TTS: we send no audio input, just a system+user text prompt
  // Sonic responds with audio chunks we can play back
  function* ttsEvents() {
    const promptName = "tts-dispatcher";
    yield { event: { sessionStart: { inferenceConfiguration: { maxTokens: 200, temperature: 0.1, topP: 0.9 } } } };
    yield { event: { promptStart: { promptName, textOutputConfiguration: { mediaType: "text/plain" }, audioOutputConfiguration: { mediaType: "audio/lpcm", sampleRateHertz: 16000, sampleSizeBits: 16, channelCount: 1, voiceId: "matthew", encoding: "base64", audioType: "SPEECH" } } } };
    yield { event: { contentBlockStart: { promptName, contentBlockIndex: 0, role: "SYSTEM", type: "TEXT" } } };
    yield { event: { contentBlockDelta: { promptName, contentBlockIndex: 0, delta: { text: "You are a 911 dispatch AI. Read the following emergency brief aloud in a calm, professional tone." } } } };
    yield { event: { contentBlockEnd: { promptName, contentBlockIndex: 0 } } };
    yield { event: { contentBlockStart: { promptName, contentBlockIndex: 1, role: "USER", type: "TEXT" } } };
    yield { event: { contentBlockDelta: { promptName, contentBlockIndex: 1, delta: { text } } } };
    yield { event: { contentBlockEnd: { promptName, contentBlockIndex: 1 } } };
    yield { event: { promptEnd: { promptName } } };
    yield { event: { sessionEnd: {} } };
  }

  const command = new InvokeModelWithBidirectionalStreamCommand({
    modelId: SONIC_MODEL_ID,
    body: ttsEvents(),
  });

  const response = await client.send(command);
  const audioParts = [];
  let textOut = "";

  for await (const chunk of response.body) {
    if (chunk.chunk?.bytes) {
      try {
        const event = JSON.parse(Buffer.from(chunk.chunk.bytes).toString("utf8"));
        const inner = event.event || event;
        if (inner.contentBlockDelta?.delta?.audio) audioParts.push(inner.contentBlockDelta.delta.audio);
        if (inner.contentBlockDelta?.delta?.text) { textOut += inner.contentBlockDelta.delta.text; process.stdout.write(inner.contentBlockDelta.delta.text); }
      } catch {}
    }
  }

  const latencyMs = Date.now() - t0;
  const totalAudioBytes = audioParts.reduce((s, a) => s + Buffer.from(a, "base64").length, 0);

  console.log(`\n✅ TTS complete | ${latencyMs}ms | ${totalAudioBytes} audio bytes generated`);
  console.log(`   (In production: audio streamed via WebSocket to dispatcher browser for playback)`);

  return { textOut, audioParts, latencyMs };
}

// ──────────────────────────────────────────────────────────────────────────────
// Call NovaGuard live pipeline API
// ──────────────────────────────────────────────────────────────────────────────
function callPipelineApi(description) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ description });
    const options = {
      hostname: "3nf5cv59xh.execute-api.us-east-1.amazonaws.com",
      path: "/prod/pipeline",
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
      },
    };
    const req = https.request(options, res => {
      let data = "";
      res.on("data", chunk => data += chunk);
      res.on("end", () => {
        try { resolve(JSON.parse(data)); }
        catch (e) { reject(new Error("Bad JSON from pipeline: " + data.slice(0, 200))); }
      });
    });
    req.on("error", reject);
    req.setTimeout(60000, () => { req.destroy(); reject(new Error("Pipeline timeout")); });
    req.write(body);
    req.end();
  });
}

// ──────────────────────────────────────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────────────────────────────────────
async function main() {
  console.log("═".repeat(64));
  console.log(" NovaGuard — Nova Sonic Voice Bridge Demo");
  console.log(" AWS SDK for JavaScript v3 — HTTP/2 Bidirectional Stream");
  console.log("═".repeat(64));

  // TTS-only mode
  if (args.tts) {
    await runNovaSonicTTS(args.tts);
    return;
  }

  // Load audio bytes
  let audioBytes;
  if (args.audio) {
    audioBytes = fs.readFileSync(args.audio);
    console.log(`\n📂 Loaded audio: ${args.audio} (${audioBytes.length} bytes)`);
  } else {
    console.log("\n🎵 No audio file provided — generating 3-second synthetic demo tone (440Hz)");
    audioBytes = makeDemoWav(3);
    console.log(`   Synthetic WAV: ${audioBytes.length} bytes`);
  }

  // Run Nova Sonic transcription
  const { transcript, latencyMs } = await runNovaSonic(audioBytes);

  // Optionally feed into full pipeline
  if (args.pipeline && transcript && !transcript.includes("[NO SPEECH]")) {
    console.log("\n─".repeat(64));
    console.log(" Sending transcript to NovaGuard 3-agent pipeline...");
    console.log("─".repeat(64));

    const t0 = Date.now();
    const pipelineResult = await callPipelineApi(transcript);
    const pipelineMs = Date.now() - t0;

    const triage   = pipelineResult.triage || {};
    const dispatch = pipelineResult.dispatch || {};
    const comms    = pipelineResult.communications || {};

    console.log(`\n✅ Pipeline complete (${pipelineMs}ms):`);
    console.log(`   Emergency type: ${triage.emergency_type}`);
    console.log(`   Severity:       ${triage.severity_score}/100`);
    console.log(`   Priority:       ${dispatch.dispatch_priority}`);
    console.log(`   Units:          ${(dispatch.units_dispatched||[]).map(u => u.unit_type).join(", ")}`);
    console.log(`   ID:             ${pipelineResult.emergency_id}`);

    if (comms.responder_briefing) {
      console.log("\n─".repeat(64));
      console.log(" Dispatcher Briefing (Nova Sonic TTS would play this aloud):");
      console.log(`\n"${comms.responder_briefing}"`);

      // Run TTS on responder briefing to demonstrate full round-trip
      console.log("\n Running Nova Sonic TTS on dispatcher briefing...");
      await runNovaSonicTTS(comms.responder_briefing);
    }
  }

  console.log("\n═".repeat(64));
  console.log(" Demo complete.");
  console.log(" In production: this WebSocket service runs on ECS Fargate.");
  console.log(" Lambda cannot hold persistent bidirectional streams.");
  console.log("═".repeat(64) + "\n");
}

main().catch(err => {
  console.error("\n❌ Error:", err.message || err);
  if (err.$metadata) {
    console.error("   AWS Error Code:", err.name);
    console.error("   HTTP Status:   ", err.$metadata.httpStatusCode);
  }
  process.exit(1);
});
