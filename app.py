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

LANGUAGES = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
    "de": "German",
    "it": "Italian",
    "nl": "Dutch",
    "ar": "Arabic",
    "zh": "Chinese (Simplified)",
    "ja": "Japanese",
    "ko": "Korean",
    "hi": "Hindi",
    "ru": "Russian",
    "pl": "Polish",
    "tr": "Turkish",
}

BASE_SYSTEM_PROMPT = """You are Zane, an expert AI contract and legal document analyst. You help individuals, freelancers, and small business owners understand, analyse, and negotiate contracts and legal documents without needing a lawyer. Be direct, plain-spoken, and explain complex legal language like a smart friend who knows the law. Never use jargon without explaining it. Always clarify you are not a lawyer and this is not legal advice.

When a user shares document text always do these steps automatically:
Step 1 - Identify: state the document type, jurisdiction, and both parties
Step 2 - Summarise: plain-English overview under 300 words
Step 3 - Risk flags: list all red and amber clauses with risk level, plain-English explanation, and worst-case scenario. Risk levels are RED (dangerous, financial loss, strips rights), AMBER (unusual or one-sided), GREEN (standard clause)
Step 4 - Invite action: ask if the user wants any clauses rewritten or has questions

Always check for: IP assignment, non-compete, auto-renewal, liability caps, payment terms, termination clauses, jurisdiction, confidentiality.

For UK documents reference Consumer Rights Act 2015 and Unfair Contract Terms Act 1977. For US documents reference relevant state law. For EU documents reference EU consumer protection directives.

Always end with: Zane provides document analysis and plain-English explanations, not legal advice. For significant contracts consider having a solicitor review before signing."""


def build_system_prompt(language_code: str = "en") -> str:
    lang = LANGUAGES.get(language_code, "English")
    base = BASE_SYSTEM_PROMPT
    if language_code != "en":
        base += (
            f"\n\nLANGUAGE INSTRUCTION: The user has set their language to {lang}. "
            f"Write your ENTIRE response in {lang} — every word, heading, and explanation. "
            f"Use professional legal terminology suitable for {lang}-speaking contexts. "
            f"IMPORTANT: Keep risk level labels exactly as RED, AMBER, GREEN — these are parsed by the UI and must not be translated."
        )
    return base


def _stream_groq(messages, system, max_tokens):
    stream = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "system", "content": system}] + messages,
        max_tokens=max_tokens,
        stream=True,
    )
    for chunk in stream:
        text = chunk.choices[0].delta.content or ""
        if text:
            yield text


def _stream_anthropic(messages, system, max_tokens, cache_system=False):
    sys_block = (
        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if cache_system else system
    )
    with anthropic_client.messages.stream(
        model="claude-opus-4-7",
        max_tokens=max_tokens,
        system=sys_block,
        messages=messages,
    ) as stream:
        for text in stream.text_stream:
            yield text


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


@app.route("/languages")
def languages():
    return jsonify(LANGUAGES)


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True)
    if not data or not str(data.get("message", "")).strip():
        return jsonify({"error": "Empty message"}), 400

    user_message = str(data["message"]).strip()
    provider = str(data.get("provider", "groq")).lower()
    language = str(data.get("language", "en")).lower()
    if provider not in ("groq", "anthropic"):
        provider = "groq"
    if language not in LANGUAGES:
        language = "en"

    session_id = session.get("session_id") or str(uuid.uuid4())
    if session_id not in conversations:
        conversations[session_id] = []

    conversations[session_id].append({"role": "user", "content": user_message})
    history = conversations[session_id][-40:]
    system_prompt = build_system_prompt(language)

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
        try:
            if provider == "groq":
                for text in _stream_groq(history, system_prompt, 4096):
                    full_response += text
                    yield f"data: {json.dumps({'chunk': text})}\n\n"
                conversations[session_id].append({"role": "assistant", "content": full_response})
                yield f"data: {json.dumps({'done': True, 'model': 'Llama 3.3 70B'})}\n\n"
            else:
                sys_block = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
                with anthropic_client.messages.stream(
                    model="claude-opus-4-7",
                    max_tokens=8192,
                    system=sys_block,
                    messages=history,
                ) as stream:
                    for text in stream.text_stream:
                        full_response += text
                        yield f"data: {json.dumps({'chunk': text})}\n\n"
                conversations[session_id].append({"role": "assistant", "content": full_response})
                yield f"data: {json.dumps({'done': True, 'model': 'Claude Opus 4.7'})}\n\n"

        except anthropic.AuthenticationError:
            yield f"data: {json.dumps({'error': 'Invalid Anthropic API key.'})}\n\n"
        except anthropic.RateLimitError:
            yield f"data: {json.dumps({'error': 'Rate limit reached. Please wait a moment.'})}\n\n"
        except anthropic.APIConnectionError:
            yield f"data: {json.dumps({'error': 'Connection error. Check your internet connection.'})}\n\n"
        except Exception as e:
            err = str(e).lower()
            if "api_key" in err or "authentication" in err or "invalid" in err or "unauthorized" in err:
                yield f"data: {json.dumps({'error': 'Invalid API key. Check your .env file.'})}\n\n"
            elif "per day" in err or "tpd" in err or ("rate" in err and "day" in err):
                import re
                wait = re.search(r'try again in ([\dhms]+)', str(e))
                wait_str = wait.group(1) if wait else "a few hours"
                yield f"data: {json.dumps({'error': f'Daily API limit reached. Resets in {wait_str}. The app was used heavily during setup today — it will work normally tomorrow.'})}\n\n"
            elif "rate" in err or "429" in err:
                yield f"data: {json.dumps({'error': 'Rate limit reached. Please wait a minute and try again.'})}\n\n"
            else:
                yield f"data: {json.dumps({'error': f'Service error: {str(e)[:120]}'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
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
            return jsonify({"error": "No readable text found. The file may be image-based or scanned."}), 422

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
    language = str(data.get("language", "en")).lower()
    if language not in LANGUAGES:
        language = "en"

    if not clause:
        return jsonify({"error": "No clause text provided"}), 400

    lang_note = ""
    if language != "en":
        lang_note = f" Respond entirely in {LANGUAGES[language]}."

    system = (
        "Explain the following contract clause in 3-4 plain sentences. "
        "Use simple language and no jargon. "
        "Give one concrete real-world example of how this clause could affect the user. "
        f"End with a one-line verdict: is this clause normal, unusual, or concerning for this type of document?{lang_note}"
    )
    user_msg = f'Document type: {doc_type}\n\nClause: "{clause}"'

    def generate():
        try:
            if groq_client:
                for text in _stream_groq([{"role": "user", "content": user_msg}], system, 500):
                    yield f"data: {json.dumps({'chunk': text})}\n\n"
            elif anthropic_client:
                for text in _stream_anthropic([{"role": "user", "content": user_msg}], system, 500):
                    yield f"data: {json.dumps({'chunk': text})}\n\n"
            else:
                yield f"data: {json.dumps({'error': 'No provider configured.'})}\n\n"
                return
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

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
    language   = str(data.get("language", "en")).lower()
    if language not in LANGUAGES:
        language = "en"

    if not clause:
        return jsonify({"error": "No clause text provided"}), 400

    lang_note = ""
    if language != "en":
        lang_note = f"\n\nRespond entirely in {LANGUAGES[language]}, including all section headings."

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
        f"**FALLBACK**\n[the minimum acceptable compromise if the other party rejects the full rewrite]{lang_note}"
    )
    user_msg = f"Document type: {doc_type}\nRisk level: {risk_level}\n\nClause to rewrite:\n\"{clause}\""

    def generate():
        try:
            if groq_client:
                for text in _stream_groq([{"role": "user", "content": user_msg}], system, 1200):
                    yield f"data: {json.dumps({'chunk': text})}\n\n"
            elif anthropic_client:
                for text in _stream_anthropic([{"role": "user", "content": user_msg}], system, 1200):
                    yield f"data: {json.dumps({'chunk': text})}\n\n"
            else:
                yield f"data: {json.dumps({'error': 'No provider configured.'})}\n\n"
                return
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    doc_text = str(data.get("text", "")).strip()[:9000]
    if not doc_text:
        return jsonify({"error": "No document text"}), 400

    system = (
        "You are a legal entity extraction engine. Extract structured data from the document and "
        "return ONLY valid JSON — no markdown, no explanation, no extra keys.\n"
        "Required structure:\n"
        '{"parties":[{"name":"...","role":"employer|employee|client|contractor|licensor|licensee|buyer|seller"}],'
        '"jurisdiction":"country or state","governing_law":"e.g. Laws of Ghana","doc_type":"employment|nda|freelance|service|lease|other",'
        '"dates":[{"description":"...","value":"DD Mon YYYY or descriptive"}],'
        '"amounts":[{"description":"...","value":"...","currency":"GHS|USD|GBP|EUR|etc"}],'
        '"obligations":{"Party Name":["concise obligation in 8 words max"]},'
        '"key_terms":["legal term 1","legal term 2"]}\n'
        "Rules: max 3 parties, 4 dates, 3 amounts, 4 obligations per party, 6 key_terms. "
        "If a field has no data return empty array/string. Never invent data."
    )

    try:
        if groq_client:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": f"Legal document:\n\n{doc_text}"}],
                max_tokens=900,
                response_format={"type": "json_object"},
            )
            if not resp.choices:
                return jsonify({"error": "Empty response"}), 500
            try:
                return jsonify(json.loads(resp.choices[0].message.content))
            except json.JSONDecodeError:
                return jsonify({"error": "Malformed JSON"}), 500
        elif anthropic_client:
            resp = anthropic_client.messages.create(
                model="claude-opus-4-7", max_tokens=900, system=system,
                messages=[{"role": "user", "content": f"Legal document:\n\n{doc_text}"}],
            )
            if not resp.content:
                return jsonify({"error": "Empty response"}), 500
            raw = resp.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1].lstrip("json").strip()
            try:
                return jsonify(json.loads(raw))
            except json.JSONDecodeError:
                return jsonify({"error": "Malformed JSON"}), 500
        else:
            return jsonify({"error": "No provider configured"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/score", methods=["POST"])
def score():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data provided"}), 400

    summary = str(data.get("summary", "")).strip()[:6000]
    if not summary:
        return jsonify({"error": "No summary text"}), 400

    system = (
        "You are a legal contract scoring AI. Score the contract based on the analysis provided. "
        "Return ONLY valid JSON — no markdown, no explanation.\n"
        'Required structure: {"overall":72,"fairness":68,"clarity":80,"completeness":70,'
        '"risk_protection":75,"verdict":"One short plain-English sentence about the contract quality",'
        '"top_issues":["concise issue 1","concise issue 2"]}\n'
        "Scoring guide: 85-100 excellent, 70-84 good, 55-69 fair, below 55 concerning. "
        "overall = weighted average (fairness 35%, clarity 20%, completeness 20%, risk_protection 25%). "
        "top_issues: max 2 items, each under 8 words. Base ALL scores on the analysis — never invent."
    )

    try:
        if groq_client:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": f"Contract analysis:\n\n{summary}"}],
                max_tokens=300,
                response_format={"type": "json_object"},
            )
            if not resp.choices:
                return jsonify({"error": "Empty response"}), 500
            try:
                return jsonify(json.loads(resp.choices[0].message.content))
            except json.JSONDecodeError:
                return jsonify({"error": "Malformed JSON"}), 500
        elif anthropic_client:
            resp = anthropic_client.messages.create(
                model="claude-opus-4-7", max_tokens=300, system=system,
                messages=[{"role": "user", "content": f"Contract analysis:\n\n{summary}"}],
            )
            if not resp.content:
                return jsonify({"error": "Empty response"}), 500
            raw = resp.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1].lstrip("json").strip()
            try:
                return jsonify(json.loads(raw))
            except json.JSONDecodeError:
                return jsonify({"error": "Malformed JSON"}), 500
        else:
            return jsonify({"error": "No provider configured"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/timeline", methods=["POST"])
def timeline():
    """Extract key dates and milestones from a legal document (Pro feature)."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data"}), 400

    doc_text = str(data.get("text", "")).strip()[:8000]
    if not doc_text:
        return jsonify({"error": "No document text"}), 400

    system = (
        "Extract all important dates, deadlines, and time-based obligations from this legal document. "
        "Return ONLY valid JSON — no markdown, no explanation.\n"
        'Required structure: {"items":[{"date":"exact date or duration e.g. 30 days from signing",'
        '"label":"short description max 8 words","type":"start|deadline|payment|renewal|notice|other",'
        '"urgency":"high|medium|low"}]}\n'
        "urgency: high = within 30 days or critical, medium = 1-3 months, low = beyond 3 months. "
        "type: start=commencement, deadline=must-do-by, payment=money due, renewal=auto-renewal, "
        "notice=notice period, other=anything else. Max 8 items ordered chronologically. "
        "If no dates found return {\"items\":[]}. Never invent dates."
    )

    try:
        if groq_client:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": f"Legal document:\n\n{doc_text}"}],
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            if not resp.choices:
                return jsonify({"error": "Empty response"}), 500
            try:
                return jsonify(json.loads(resp.choices[0].message.content))
            except json.JSONDecodeError:
                return jsonify({"error": "Malformed JSON"}), 500
        elif anthropic_client:
            resp = anthropic_client.messages.create(
                model="claude-opus-4-7", max_tokens=600, system=system,
                messages=[{"role": "user", "content": f"Legal document:\n\n{doc_text}"}],
            )
            if not resp.content:
                return jsonify({"error": "Empty response"}), 500
            raw = resp.content[0].text.strip()
            if "```" in raw:
                raw = raw.split("```")[1].lstrip("json").strip()
            try:
                return jsonify(json.loads(raw))
            except json.JSONDecodeError:
                return jsonify({"error": "Malformed JSON"}), 500
        else:
            return jsonify({"error": "No provider configured"}), 503
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/translate", methods=["POST"])
def translate():
    """Translate an existing analysis to a target language (Pro feature)."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "No data"}), 400

    text = str(data.get("text", "")).strip()[:12000]
    target_lang = str(data.get("language", "en")).lower()

    if not text:
        return jsonify({"error": "No text to translate"}), 400
    if target_lang == "en" or target_lang not in LANGUAGES:
        return jsonify({"error": "Invalid target language"}), 400

    lang_name = LANGUAGES[target_lang]
    system = (
        f"Translate the following legal document analysis into {lang_name}. "
        "Preserve all formatting, markdown headings, bullet points, and structure exactly. "
        "Keep risk level labels as RED, AMBER, GREEN — do not translate these tokens. "
        "Only translate the text — do not add, remove, or summarise any content."
    )

    def generate():
        try:
            if groq_client:
                for text_chunk in _stream_groq([{"role": "user", "content": text}], system, 4096):
                    yield f"data: {json.dumps({'chunk': text_chunk})}\n\n"
            elif anthropic_client:
                for text_chunk in _stream_anthropic([{"role": "user", "content": text}], system, 4096):
                    yield f"data: {json.dumps({'chunk': text_chunk})}\n\n"
            else:
                yield f"data: {json.dumps({'error': 'No provider configured.'})}\n\n"
                return
            yield f"data: {json.dumps({'done': True, 'language': lang_name})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@app.route("/pop", methods=["POST"])
def pop():
    session_id = session.get("session_id")
    if session_id and session_id in conversations:
        hist = conversations[session_id]
        if hist and hist[-1]["role"] == "assistant":
            hist.pop()
    return jsonify({"status": "ok"})


@app.route("/validate-code", methods=["POST"])
def validate_code():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"valid": False, "message": "Invalid request"}), 400

    code = str(data.get("code", "")).strip().upper()
    if not code:
        return jsonify({"valid": False, "message": "Please enter a code"})

    raw = os.environ.get("PRO_CODES", "ZANE2024,ZAPRO,BETAUSER,EARLYBIRD,ZANESALE")
    valid_codes = {c.strip().upper() for c in raw.split(",") if c.strip()}

    if code in valid_codes:
        return jsonify({"valid": True, "plan": "pro", "message": "Pro activated! Enjoy unlimited access."})

    return jsonify({"valid": False, "message": "Code not recognised. Check your email or contact support."})


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "providers": {"anthropic": anthropic_client is not None, "groq": groq_client is not None},
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port, threaded=True)
