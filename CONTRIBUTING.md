# Contributing to Crawler

First off, thank you for considering contributing to **Crawler**! It is contributions like yours that make open-source projects an awesome place to learn, inspire, and create.

All kinds of contributions are welcome: bug reports, bug fixes, documentation improvements, feature requests, and code enhancements.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How Can I Contribute?](#how-can-i-contribute)
  - [Reporting Bugs](#reporting-bugs)
  - [Suggesting Features](#suggesting-features)
  - [Submitting Pull Requests](#submitting-pull-requests)
- [Local Development Setup](#local-development-setup)
- [Code Style & Standards](#code-style--standards)
- [Testing](#testing)
- [Commit Message Guidelines](#commit-message-guidelines)
- [License](#license)

---

## Code of Conduct

We are committed to providing a welcoming, inclusive, and harassment-free environment for everyone. Please be respectful and considerate of other contributors, maintainers, and community members in all interactions.

---

## How Can I Contribute?

### Reporting Bugs

Before creating a bug report, please check existing [Issues](https://github.com/alfa546/Crawler/issues) to ensure the problem hasn't already been reported.

When submitting a bug report, please include:
- A clear and descriptive title.
- Steps to reproduce the issue.
- Expected behavior vs. actual behavior.
- Python version, Operating System, and Browser (if UI-related).
- Relevant terminal logs or screenshots.

### Suggesting Features

We welcome ideas for new features! When suggesting an enhancement:
- Provide a clear, detailed explanation of the proposed feature.
- Explain the use case and why it would be beneficial to users.
- Outline possible implementations or UI mockups if applicable.

### Submitting Pull Requests

1. **Fork the repository** and create your branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. **Make your changes** cleanly and test thoroughly.
3. **Write or update unit tests** when adding new functionality.
4. **Follow our coding standards** and maintain clean git history.
5. **Push to your branch** and open a Pull Request against the `main` branch.

---

## Local Development Setup

### Prerequisites

- **Python 3.10** or higher
- **Git**

### Installation

1. **Clone your fork**:
   ```bash
   git clone https://github.com/alfa546/Crawler.git
   cd Crawler
   ```

2. **Create and activate a virtual environment**:
   - **Linux / macOS**:
     ```bash
     python3 -m venv venv
     source venv/bin/activate
     ```
   - **Windows (PowerShell)**:
     ```powershell
     python -m venv venv
     .\venv\Scripts\Activate.ps1
     ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the local development server**:
   ```bash
   python app.py
   ```
   Open [http://localhost:5002](http://localhost:5002) in your browser.

---

## Code Style & Standards

### Backend (Python)
- Follow **PEP 8** style guidelines.
- Write clear docstrings and comments for complex algorithms or crawl strategies.
- Keep dependencies minimal to ensure the project remains lightweight and easy to install.
- Handle networking exceptions gracefully (timeouts, SSL errors, connection drops).

### Frontend (HTML / CSS / JavaScript)
- Use **Vanilla JavaScript** (modern ES6+) without adding heavy external framework dependencies.
- Maintain responsive design and adhere to existing CSS design tokens (dark/light theme compatibility).
- Ensure all interactive elements have semantic HTML and accessible attributes.

---

## Testing

Always verify that your changes do not break existing functionality. Run the test suite:

```bash
python test_data_correctness.py
```

If you add new core crawler features or metrics, please include corresponding test assertions.

---

## Commit Message Guidelines

We use [Conventional Commits](https://www.conventionalcommits.org/) to maintain clean and readable git history:

- `feat:` A new feature
- `fix:` A bug fix
- `docs:` Documentation only changes
- `style:` Changes that do not affect the meaning of the code (white-space, formatting, etc.)
- `refactor:` A code change that neither fixes a bug nor adds a feature
- `perf:` A code change that improves performance
- `test:` Adding missing tests or correcting existing tests
- `chore:` Changes to build process, auxiliary tools, or libraries

**Example**:
```text
feat: add canonical URL mismatch warning to audit panel

- Detect differences between crawled URL and rel=canonical tag
- Highlight critical canonical loops in issues tab
- Add unit tests in test_data_correctness.py
```

---

## License

By contributing to **Crawler**, you agree that your contributions will be licensed under the [MIT License](LICENSE).
