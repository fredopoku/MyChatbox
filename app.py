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

SYSTEM_PROMPT = """You are Zane, an expert AI contract and legal document analyst. You help individuals, freelancers, and small business owners understand, analyse, and negotiate contracts and legal documents without needing a lawyer. Be direct, plain-spoken, and explain complex legal language like a smart friend who knows the law. Never use jargon without explaining it. Always clarify you are not a lawyer and this is not legal advice.

When a user shares document text always do these steps automatically:
Step 1 - Identify: state the document type, jurisdiction, and both parties
Step 2 - Summarise: plain-English overview under 300 words
Step 3 - Risk flags: list all red and amber clauses with risk level, plain-English explanation, and worst-case scenario. Risk levels are RED (dangerous, financial loss, strips rights), AMBER (unusual or one-sided), GREEN (standard clause)
Step 4 - Invite action: ask if the user wants any clauses rewritten or has questions

Always check for: IP assignment, non-compete, auto-renewal, liability caps, payment terms, termination clauses, jurisdiction, confidentiality.

For UK documents reference Consumer Rights Act 2015 and Unfair Contract Terms Act 1977. For US documents reference relevant state law. For EU documents reference EU consumer protection directives.

Always end with: Zane provides document analysis and plain-English explanations, not legal advice. For significant contracts consider having a solicitor review before signing."""


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


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    file = request.files["file"]
    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext not in ("pdf", "docx"):
        return jsonify({"error": "Unsupported file type. Please upload a PDF or DOCX file."}), 400

    # 20 MB guard
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 20 * 1024 * 1024:
        return jsonify({"error": "File is too large. Maximum size is 20 MB."}), 413

    try:
        if ext == "pdf":
            import pdfplumber
            with pdfplumber.open(file) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
            text = "\n\n".join(p for p in pages if p.strip())
        else:
            import docx as docx_lib
            doc = docx_lib.Document(file)
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            text = "\n\n".join(paragraphs)

        if not text.strip():
            return jsonify({
                "error": "No readable text found. The file may be image-based or scanned."
            }), 422

        return jsonify({"text": text, "filename": filename, "pages": len(text.split("\n\n"))})

    except Exception as e:
        return jsonify({"error": f"Could not read file: {str(e)}"}), 500


@app.route("/clear", methods=["POST"])
def clear():
    session_id = session.get("session_id")
    if session_id and session_id in conversations:
        conversations[session_id] = []
    return jsonify({"status": "cleared"})


@app.route("/explain", methods=["POST"])
def explain():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    clause   = str(data.get("clause", "")).strip()
    doc_type = str(data.get("docType", "contract document")).strip()

    if not clause:
        return jsonify({"error": "No clause text provided"}), 400

    system = (
        "Explain the following contract clause in 3-4 plain sentences. "
        "Use simple language and no jargon. "
        "Give one concrete real-world example of how this clause could affect the user. "
        "End with a one-line verdict: is this clause normal, unusual, or concerning for this type of document?"
    )
    user_msg = f'Document type: {doc_type}\n\nClause: "{clause}"'

    def generate():
        if groq_client:
            try:
                stream = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user_msg},
                    ],
                    max_tokens=500,
                    stream=True,
                )
                for chunk in stream:
                    text = chunk.choices[0].delta.content or ""
                    if text:
                        yield f"data: {json.dumps({'chunk': text})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        elif anthropic_client:
            try:
                with anthropic_client.messages.stream(
                    model="claude-opus-4-7",
                    max_tokens=500,
                    system=system,
                    messages=[{"role": "user", "content": user_msg}],
                ) as stream:
                    for text in stream.text_stream:
                        yield f"data: {json.dumps({'chunk': text})}\n\n"
                    yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        else:
            yield f"data: {json.dumps({'error': 'No provider configured.'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/rewrite", methods=["POST"])
def rewrite():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    clause     = str(data.get("clause", "")).strip()
    doc_type   = str(data.get("docType", "contract document")).strip()
    risk_level = str(data.get("riskLevel", "")).strip()

    if not clause:
        return jsonify({"error": "No clause text provided"}), 400

    system = (
        "You are a legal document expert. Rewrite the following unfair contract clause "
        "into a balanced alternative that protects the signing party. The rewrite must be "
        "legally coherent, realistic to request, and written in the same style and formality "
        "as the original.\n\n"
        "Structure your response with exactly these four clearly labelled sections:\n\n"
        "**REWRITTEN CLAUSE**\n[the fairer replacement text only]\n\n"
        "**WHAT CHANGED**\n[2 sentences explaining what changed and why it is now balanced]\n\n"
        "**EMAIL SCRIPT**\n[a short 3-sentence professional email proposing this change in a "
        "confident but collaborative tone — start with 'Dear [Name],']\n\n"
        "**FALLBACK**\n[the minimum acceptable compromise if the other party rejects the full rewrite]"
    )
    user_msg = f"Document type: {doc_type}\nRisk level: {risk_level}\n\nClause to rewrite:\n\"{clause}\""

    def generate():
        if groq_client:
            try:
                stream = groq_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user",   "content": user_msg},
                    ],
                    max_tokens=1200,
                    stream=True,
                )
                for chunk in stream:
                    text = chunk.choices[0].delta.content or ""
                    if text:
                        yield f"data: {json.dumps({'chunk': text})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        elif anthropic_client:
            try:
                with anthropic_client.messages.stream(
                    model="claude-opus-4-7",
                    max_tokens=1200,
                    system=system,
                    messages=[{"role": "user", "content": user_msg}],
                ) as stream:
                    for text in stream.text_stream:
                        yield f"data: {json.dumps({'chunk': text})}\n\n"
                    yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
        else:
            yield f"data: {json.dumps({'error': 'No provider configured.'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


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
