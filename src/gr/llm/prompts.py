"""Instructions the LLM client adds on its own: output repair and the json_object fallback.

Task prompts live in `extraction/prompts/*.md`; a change here is a prompt change (eval first).
"""

REPAIR_INSTRUCTION = (
    "Предыдущий ответ не прошёл проверку по JSON-схеме. Ошибки:\n{errors}\n"
    "Верни только исправленный JSON строго по схеме, без пояснений."
)
JSON_OBJECT_INSTRUCTION = "\n\nОтвет — один JSON-объект строго по этой JSON-схеме:\n{schema}"
