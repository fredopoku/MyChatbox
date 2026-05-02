from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# Homepage route
@app.route("/")
def index():
    return render_template("index.html")  # This must match your file name in templates/

# Chatbot logic
@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json["message"].lower()

    # Rule-based chatbot responses
    responses = {
        "hello": "Hi there! How can I assist you?",
        "how are you?": "I'm just a bot, but I'm doing great! How about you?",
        "what is your name?": "I am a chatbot built by Fred!",
        "bye": "Goodbye! Have a great day!",   
        "how is life?": "I have no idea, I've just been living... lol",
    }

    bot_response = responses.get(user_message, "I'm sorry, I don't understand that.")
    
    return jsonify({"response": bot_response})

if __name__ == "__main__":
    app.run(debug=True)
