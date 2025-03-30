from flask import Flask, render_template, request
import openai
import os

app = Flask(__name__)

openai.api_key = os.getenv("OPENAI_API_KEY")

DEFAULT_QUESTIONS = [
    "What are the strengths of this company?",
    "What are the challenges this company is facing?",
    "What trends are impacting this company or its industry?"
]

@app.route("/", methods=["GET", "POST"])
def index():
    answers = []
    if request.method == "POST":
        company = request.form["company"]
        for q in DEFAULT_QUESTIONS:
            prompt = f"{q} The company is {company}."
            try:
                response = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7
                )
                answer = response["choices"][0]["message"]["content"]
                print(f"✅ GPT 回應：{answer}")
                answers.append({"question": q, "answer": answer})
            except Exception as e:
                print(f"❌ 錯誤
