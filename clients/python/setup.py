"""setuptools shim so ``pip install .`` works without Poetry; metadata mirrors pyproject.toml."""
from setuptools import find_packages, setup

setup(
    name="autobuy-client",
    version="0.4.1",
    description="Type-safe Python client for the AutoBuy Crypto API (generated from OpenAPI)",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="AutoBuy support",
    author_email="support@minastech.ca",
    license="MIT",
    packages=find_packages(exclude=("tests",)),
    package_data={"autobuy_client": ["py.typed"]},
    python_requires=">=3.9",
    install_requires=["httpx>=0.24,<1.0", "typing-extensions>=4.4; python_version<'3.11'"],
    extras_require={"dev": ["pytest>=7.0", "pytest-asyncio>=0.21"]},
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Typing :: Typed",
    ],
)
