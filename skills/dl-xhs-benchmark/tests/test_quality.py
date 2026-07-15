import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.utils import quality


def test_check_outputs_reports_missing_html(tmp_path):
    skill_dir = tmp_path / "博主A_写作指南.skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 写作指南\n内容", encoding="utf-8")

    issues = quality.check_outputs(str(tmp_path), "博主A")
    assert any("拆解报告.html" in issue for issue in issues)


def test_check_outputs_reports_skill_as_file_not_folder(tmp_path):
    (tmp_path / "博主A_拆解报告.html").write_text(
        "<html>框架 标题 开头 中间 结尾 CTA</html>", encoding="utf-8"
    )
    (tmp_path / "博主A_写作指南.skill").write_text("不应该是文件", encoding="utf-8")

    issues = quality.check_outputs(str(tmp_path), "博主A")
    assert any("必须是文件夹" in issue for issue in issues)


def test_check_outputs_passes_when_all_valid(tmp_path):
    (tmp_path / "博主A_拆解报告.html").write_text(
        "<html>框架 标题 开头 中间 结尾 CTA</html>", encoding="utf-8"
    )
    skill_dir = tmp_path / "博主A_写作指南.skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 写作指南\n内容", encoding="utf-8")

    issues = quality.check_outputs(str(tmp_path), "博主A")
    assert issues == []
