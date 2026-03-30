"""Wrapper entrypoint for the existing Gemini research workflow.

This keeps implementation in new files and leaves main.py untouched.
"""

from gemini_research_loop import main as research_main


if __name__ == "__main__":
    research_main()
