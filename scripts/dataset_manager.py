import json
import os

source_file = 'dataclaude/hmm_dataset/hinglishmath_1k2.jsonl'
verified_file = 'dataclaude/hmm_dataset/hinglishmath_1k2_verified.jsonl'

def get_processed_ids():
    if not os.path.exists(verified_file):
        return set()
    ids = set()
    with open(verified_file, 'r') as f:
        for line in f:
            data = json.loads(line)
            ids.add(data['id'])
    return ids

def get_next_batch(size=50):
    processed = get_processed_ids()
    batch = []
    with open(source_file, 'r') as f:
        for line in f:
            data = json.loads(line)
            if data['id'] not in processed:
                batch.append(data)
                if len(batch) >= size:
                    break
    return batch

if __name__ == "__main__":
    batch = get_next_batch(10)
    for item in batch:
        print(f"ID: {item['id']}")
        print(f"Problem: {item['variants']['EN']['problem']}")
        print("-" * 20)
