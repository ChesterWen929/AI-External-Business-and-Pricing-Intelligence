from flask import Flask, render_template, request
from openai import OpenAI
import os

app = Flask(__name__)

# 初始化新版 OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

DEFAULT_QUESTIONS = [
    "What recent price competition is this company facing in the market?",
    "How has this company’s stock price changed recently, and what are the main reasons?",
    "What semiconductor-specific risks and opportunities has this company recently encountered?"
]

@app.route("/", methods=["GET", "POST"])
def index():
    answers = []
    if request.method == "POST":
        company = request.form["company"]
        for q in DEFAULT_QUESTIONS:
            prompt = f"{q} The company is {company}."
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7
                )
                answer = response.choices[0].message.content
                print(f"[GPT Response] {answer}")
                answers.append({"question": q, "answer": answer})
            except Exception as e:
                print(f"[ERROR] {e}")
                answers.append({"question": q, "answer": f"[Error] Cannot get response: {e}"})
    return render_template("index.html", answers=answers)
