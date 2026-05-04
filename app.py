import json
import os
import secrets
import uuid

import anthropic
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, session
from flask import stream_with_context

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

_anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
_groq_key = os.environ.get("GROQ_API_KEY", "")

_has_anthropic = bool(_anthropic_key and _anthropic_key != "your_anthropic_api_key_here")
_has_groq = bool(_groq_key and _groq_key != "your_groq_api_key_here")

anthropic_client = anthropic.Anthropic(api_key=_anthropic_key) if _has_anthropic else None

groq_client = None
try:
    from groq import Groq
    if _has_groq:
        groq_client = Groq(api_key=_groq_key)
except ImportError:
    pass

# In-memory conversation store keyed by session_id.
conversations: dict[str, list] = {}

SYSTEM_PROMPT = """You are JKRAA, a helpful, knowledgeable, and friendly AI assistant built by Fred.

You provide clear, accurate, and thoughtful responses. You can help with:
- Programming and debugging across all languages
- Data analysis and mathematics
- Writing, editing, and summarisation
- Research and factual questions
- Creative brainstorming and ideation
- Explanations of complex topics

Guidelines:
- Format responses with Markdown when it improves clarity (headings, lists, bold, etc.)
- Always use fenced code blocks with the language name (e.g. ```python) for any code
- Be concise and direct; avoid unnecessary filler phrases
- If you are uncertain, say so honestly rather than guessing"""


@app.route("/")
def index():
    if "session_id" not in session:
        session["session_id"] = str(uuid.uuid4())
    return render_template("index.html")


@app.route("/providers")
def providers():
    return jsonify({
        "anthropic": anthropic_client is not None,
        "groq": groq_client is not None,
    })


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True)
    if not data or not str(data.get("message", "")).strip():
        return jsonify({"error": "Empty message"}), 400

    user_message = str(data["message"]).strip()
    provider = str(data.get("provider", "groq")).lower()
    session_id = session.get("session_id") or str(uuid.uuid4())

    if session_id not in conversations:
        conversations[session_id] = []

    conversations[session_id].append({"role": "user", "content": user_message})
    history = conversations[session_id][-40:]

    # Fall back if requested provider has no key configured
    if provider == "groq" and groq_client is None:
        if anthropic_client is None:
            conversations[session_id].pop()
            return jsonify({"error": "No provider configured. Add GROQ_API_KEY or ANTHROPIC_API_KEY to .env"}), 503
        provider = "anthropic"
    elif provider == "anthropic" and anthropic_client is None:
        if groq_client is None:
            conversations[session_id].pop()
            return jsonify({"error": "No provider configured. Add GROQ_API_KEY or ANTHROPIC_API_KEY to .env"}), 503
        provider = "groq"

    def generate():
        full_response = ""

        if provider == "groq":
            try:
                stream = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
                    max_tokens=8192,
                    stream=True,
                )
                for chunk in stream:
                    text = chunk.choices[0].delta.content or ""
                    if text:
                        full_response += text
                        yield f"data: {json.dumps({'chunk': text})}\n\n"

                conversations[session_id].append({"role": "assistant", "content": full_response})
                yield f"data: {json.dumps({'done': True, 'model': 'Llama 3.3 70B'})}\n\n"

            except Exception as e:
                err = str(e).lower()
                if "api_key" in err or "authentication" in err or "invalid" in err or "unauthorized" in err:
                    yield f"data: {json.dumps({'error': 'Invalid Groq API key. Set GROQ_API_KEY in your .env file.'})}\n\n"
                elif "rate" in err:
                    yield f"data: {json.dumps({'error': 'Rate limit reached. Please wait a moment.'})}\n\n"
                elif "connection" in err:
                    yield f"data: {json.dumps({'error': 'Connection error. Check your internet connection.'})}\n\n"
                else:
                    yield f"data: {json.dumps({'error': 'Groq service error. Please try again.'})}\n\n"

        else:  # anthropic
            try:
                with anthropic_client.messages.stream(
                    model="claude-opus-4-7",
                    max_tokens=8192,
                    system=[
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=history,
                ) as stream:
                    for text in stream.text_stream:
                        full_response += text
                        yield f"data: {json.dumps({'chunk': text})}\n\n"

                    conversations[session_id].append({"role": "assistant", "content": full_response})
                    yield f"data: {json.dumps({'done': True, 'model': 'Claude Opus 4.7'})}\n\n"

            except anthropic.AuthenticationError:
                yield f"data: {json.dumps({'error': 'Invalid Anthropic API key. Set ANTHROPIC_API_KEY in your .env file.'})}\n\n"
            except anthropic.RateLimitError:
                yield f"data: {json.dumps({'error': 'Rate limit reached. Please wait a moment and try again.'})}\n\n"
            except anthropic.APIConnectionError:
                yield f"data: {json.dumps({'error': 'Connection error. Check your internet connection.'})}\n\n"
            except anthropic.BadRequestError as e:
                yield f"data: {json.dumps({'error': f'Bad request: {e.message}'})}\n\n"
            except Exception:
                yield f"data: {json.dumps({'error': 'An unexpected error occurred. Please try again.'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/clear", methods=["POST"])
def clear():
    session_id = session.get("session_id")
    if session_id and session_id in conversations:
        conversations[session_id] = []
    return jsonify({"status": "cleared"})


@app.route("/pop", methods=["POST"])
def pop():
    """Remove the last assistant message so the client can regenerate it."""
    session_id = session.get("session_id")
    if session_id and session_id in conversations:
        hist = conversations[session_id]
        if hist and hist[-1]["role"] == "assistant":
            hist.pop()
    return jsonify({"status": "ok"})


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "providers": {
            "anthropic": anthropic_client is not None,
            "groq": groq_client is not None,
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)
