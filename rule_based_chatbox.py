from flask import Flask, render_template, request, jsonify
import random

app = Flask(__name__)

# Homepage route
@app.route("/")
def index():
    return render_template("index.html")  # This must match your file name in templates/

# Chatbot logic
@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json["message"].lower()

    # Rule-based chatbot with randomized responses
    responses = {
        "hello": ["Hi there! 👋", "Hello! How can I assist you? 😊", "Hey! What's up?"],
        "how are you?": ["I'm great! How about you?", "I'm just a bot, but I'm doing fine!"],
        "what is your name?": ["I'm Chatbot 2.0 🤖", "I am your friendly AI assistant!"],
        "bye": ["Goodbye! Have a great day! 👋", "See you later! Take care! 😊"],
        "how is life?": ["I have no idea, I've just been living... lol", "Life is great when chatting with you! 😉"],
        "tell me a joke": [
            "Why don’t skeletons fight each other? Because they don’t have the guts! 😂",
            "Why did the math book look sad? Because it had too many problems! 🤣"
        ],
        "give me a fact": [
            "Did you know? The shortest war in history lasted only 38 to 45 minutes! ⏳",
            "Fun fact: Honey never spoils. Archaeologists found honey in ancient Egyptian tombs that was still edible! 🍯"
        ]
    }

    # Handling partial matches (e.g., "tell me a joke" and "joke")
    for key in responses:
        if key in user_message:
            bot_response = random.choice(responses[key])
            break
    else:
        bot_response = "I'm sorry, I don't understand that. 😅"

    return jsonify({"response": bot_response})

if __name__ == "__main__":
    app.run(debug=True)
