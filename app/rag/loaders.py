from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document


class KnowledgeLoader:
    PROJECT_INCLUDE_DIRS = (
        "docs",
        "design",
        "ai",
        "scripts/character_ai",
        "levels/outing",
        "components",
        "controllers",
    )
    ALLOWED_SUFFIXES = {".md", ".gd", ".tres", ".tscn", ".json", ".cfg"}
    MAX_FILE_BYTES = 256_000

    def __init__(self, knowledge_dir: str | Path, *, include_project_tree: bool = False) -> None:
        self.knowledge_dir = Path(knowledge_dir)
        self.include_project_tree = include_project_tree

    def load(self) -> list[Document]:
        if not self.knowledge_dir.exists():
            return []
        docs: list[Document] = []
        for path in self._iter_candidate_paths():
            text = self._read_text(path).strip()
            if not text:
                continue
            source = path.relative_to(self.knowledge_dir).as_posix()
            docs.append(
                Document(
                    page_content=text,
                    metadata={
                        "source": source,
                        "category": self._category_for(path),
                        "extension": path.suffix.lower(),
                    },
                )
            )
        return docs

    def _iter_candidate_paths(self) -> list[Path]:
        if not self.include_project_tree:
            return sorted(path for path in self.knowledge_dir.glob("*.md") if path.is_file())
        paths: list[Path] = []
        for relative_dir in self.PROJECT_INCLUDE_DIRS:
            root = self.knowledge_dir / relative_dir
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix.lower() not in self.ALLOWED_SUFFIXES:
                    continue
                try:
                    if path.stat().st_size > self.MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                paths.append(path)
        return sorted(paths)

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    def _category_for(self, path: Path) -> str:
        relative = ""
        try:
            relative = path.relative_to(self.knowledge_dir).as_posix().lower()
        except ValueError:
            relative = path.as_posix().lower()
        name = path.stem.lower()
        if relative.startswith("ai/"):
            return "ai_contract"
        if relative.startswith("scripts/character_ai/"):
            return "character_ai_code"
        if relative.startswith("levels/outing/"):
            return "outing_map_code"
        if relative.startswith("docs/") or relative.startswith("design/"):
            return "design_doc"
        if "mirdo" in name or "action_sheet" in name or "actions" in name:
            return "character_actions"
        if "persona" in name or "xiaokong" in name or "小空" in name:
            return "persona"
        return "world"
