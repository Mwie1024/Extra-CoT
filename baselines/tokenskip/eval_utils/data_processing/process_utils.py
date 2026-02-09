import regex

from data_processing.answer_extraction import extract_math_answer, strip_string

def process_gsm8k_test(item):
    sample = {
        'dataset': 'gsm8k-cot',
        'id': item['id'],
        'messages': [
            {'role': 'user', 'content': item['question']},
            {'role': 'assistant', 'content': regex.sub(r"<<[^<>]*>>", "", item['cot']) + "\nSo the answer is $\\boxed{" + item['answer'].strip() + "}$."}
        ],
        'answer': item['answer'].replace(',', '')
    }
    yield sample

def process_math_test(item):
    question = item["problem"]
    try:
        answer = extract_math_answer(question, item['solution'], task="cot")
    except:
        return
    sample = {
        "dataset": "math-cot",
        "id": item['id'],
        "level": item["level"],
        "type": item["type"],
        # "category": item["category"],
        "messages": [
            {"role": "user", "content": question},
            {"role": "assistant", "content": "\n".join(regex.split(r"(?<=\.) (?=[A-Z])", item["solution"]))}
        ],
        "answer": answer
    }
    yield sample
