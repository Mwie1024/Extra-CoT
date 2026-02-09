#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Quick test script: check if GPT-4o API can be called successfully.
"""


from openai import OpenAI
client = OpenAI(
    base_url='https://api.nuwaapi.com/v1',
    # sk-xxx替换为自己的key
    api_key='sk-IVrqJx8uyTVXGxhRUMX69yWzYC0HISOVSlYjan0N6MTM0HG1'
)
completion = client.chat.completions.create(
  model="gpt-4o",
  messages=[
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ]
)
print(completion.choices[0].message)