import json
import os

verified_file = 'dataclaude/hmm_dataset/hinglishmath_1k2_verified.jsonl'

def append_results(results):
    with open(verified_file, 'a') as f:
        for item in results:
            f.write(json.dumps(item) + '\n')

if __name__ == "__main__":
    # Example usage
    # append_results([{'id': 'HM-0001', 'gold_answer': '6611.57', 'gold_answer_num': 6611.57, ...}])
    pass
