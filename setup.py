from setuptools import find_packages, setup

setup(
    name="lumen-cli",
    version="0.1.0",
    description="Pure-Python ncurses harness for chatting with local Ollama models.",
    packages=find_packages(include=["lumen", "lumen.*"]),
    install_requires=[
        "requests>=2.31",
        "ddgs>=9.0",
    ],
    entry_points={
        "console_scripts": [
            "lumen=lumen.app:main",
        ],
    },
    python_requires=">=3.9",
)
