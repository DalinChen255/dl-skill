import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.utils import quality


def test_check_outputs_reports_missing_html(tmp_path):
    skill_dir = tmp_path / "博主A_创作指南.skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 创作指南\n内容", encoding="utf-8")

    issues = quality.check_outputs(str(tmp_path), "博主A")
    assert any("蒸馏报告.html" in issue for issue in issues)


def test_check_outputs_reports_skill_as_file_not_folder(tmp_path):
    (tmp_path / "博主A_蒸馏报告.html").write_text(
        "<html>IP定位 运营策略 内容形式</html>", encoding="utf-8"
    )
    (tmp_path / "博主A_创作指南.skill").write_text("不应该是文件", encoding="utf-8")

    issues = quality.check_outputs(str(tmp_path), "博主A")
    assert any("必须是文件夹" in issue for issue in issues)


def test_check_outputs_passes_when_all_valid(tmp_path):
    (tmp_path / "博主A_蒸馏报告.html").write_text(
        "<html>IP定位 运营策略 内容形式</html>", encoding="utf-8"
    )
    skill_dir = tmp_path / "博主A_创作指南.skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 创作指南\n内容", encoding="utf-8")

    issues = quality.check_outputs(str(tmp_path), "博主A")
    assert issues == []
