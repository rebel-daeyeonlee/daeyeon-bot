"""Prompt assembly for the two `pr_autofix` Claude stages (feature 004).

Kept apart from `pr_autofix.py` so prompt tuning never touches control flow.

Stage 1 — TRIAGE. The hard part is not getting Claude to fix things; it is
getting it to REFUSE. Models are compliant by default, so a naive "여기 리뷰
코멘트가 있으니 고쳐줘" yields an `accepted` verdict on essentially everything,
including the wrong ones — which is precisely the failure mode the operator
asked to avoid ("이게 진짜 고칠만한 가치가 있는지 확인해야함"). Three things push
back against that compliance:
  * an explicit catalogue of legitimate rejection reasons, so "reject" is a
    named move rather than an act of defiance;
  * `evidence` — a claim must be checkable against the diff that is in front
    of the model;
  * `deferred` as the default under uncertainty, so the model is not forced to
    choose between a wrong fix and a wrong dismissal.

Stage 2 — FIX. The agent gets the accepted instructions only. It never sees the
rejected ones, so it cannot be talked into scope creep by a comment the triage
stage already declined.
"""

from __future__ import annotations

import json
import re

from daeyeon_bot.core.pr_autofix.types import FeedbackComment, TriageDecision
from daeyeon_bot.handlers.pr_autofix_schemas import TriageOutput

# Cap per comment body. Some review bots emit multi-KB findings with full
# rendered tables; the tail is boilerplate ("_Committable suggestion_", rating
# footers) and crowds out the actual diff context.
_MAX_COMMENT_CHARS = 4000
_MAX_HUNK_CHARS = 2000


TRIAGE_DIRECTIVE = (
    "\n\n---\n\n"
    "당신은 daeyeon-bot의 **PR autofix triage** 단계로 호출되었습니다.\n"
    "아래 PR에 달린 리뷰 코멘트 각각에 대해, **정말 고칠 가치가 있는지**를"
    " 판정하세요. 코드를 직접 수정하지는 않습니다 — 판정만 합니다.\n\n"
    "## 판정 기준\n"
    "각 코멘트에 `accepted` / `rejected` / `deferred` 중 하나를 부여합니다.\n\n"
    "**`accepted`** — 지적이 옳고, 이 PR 범위 안에서 기계적으로 고칠 수 있다.\n"
    "  - `fix_instruction`에 *무엇을 어떻게* 바꿀지 구체적으로 적습니다."
    " 파일/함수/조건까지 특정하세요. '리팩터링하라' 같은 지시는 무효입니다.\n"
    "  - `evidence`에 근거가 되는 `파일:라인` 또는 인용 코드를 남깁니다.\n\n"
    "**`rejected`** — 지적이 틀렸거나 적용할 필요가 없다. 아래는 정당한"
    " 거절 사유입니다. 하나에 해당하면 주저 말고 거절하세요:\n"
    "  1. **사실 오류** — 봇이 diff를 잘못 읽었다. 지적한 코드가 그렇게 동작하지 않는다.\n"
    "  2. **이미 처리됨** — 같은 파일의 다른 곳, 호출부, 또는 타입 시스템이 이미 막고 있다.\n"
    "  3. **의도된 설계** — 프로젝트 규약(CLAUDE.md / 주변 코드 관용구)이 그 형태를 요구한다.\n"
    "  4. **범위 밖** — 이 PR이 건드리지 않은 코드에 대한 지적이다. 별도 PR 사안.\n"
    "  5. **무의미한 스타일** — 포매터/린터가 통과시킨 것에 대한 취향 문제.\n"
    "  6. **회귀 위험이 이득보다 큼** — 고치면 동작이 바뀌고, 지적된 이득은 이론적이다.\n"
    "  - `reasoning`에 위 번호가 아니라 **그 PR의 실제 코드를 근거로** 설명하세요."
    " 이 문장은 GitHub에 그대로 게시되어 리뷰어가 읽습니다.\n\n"
    "**`deferred`** — 지적은 타당하지만 기계적으로 고칠 수 없다."
    " 설계 결정이 필요하거나, 작성자 의도를 알아야 하거나, 다른 PR과 얽혀 있다.\n"
    "  - **확신이 서지 않으면 `accepted`가 아니라 `deferred`를 고르세요.**"
    " 잘못된 수정을 push하는 비용이, 사람에게 넘기는 비용보다 큽니다.\n\n"
    "## 하지 말 것\n"
    "- 리뷰어를 기쁘게 하려고 동의하지 마세요. 근거 없는 `accepted`가 이 봇의"
    " 최대 실패 모드입니다.\n"
    "- 하나의 코멘트를 여러 판정으로 쪼개지 마세요. 코멘트 하나 = 판정 하나입니다.\n"
    "- 제시된 `comment_id` 외의 id를 만들어내지 마세요.\n\n"
    "## 출력\n"
    "아래 JSON 스키마에 **정확히** 맞는 JSON 객체 하나만 출력합니다."
    " 앞뒤 산문 없이, 코드 펜스 없이, JSON만.\n"
    "`reasoning`은 한국어로 씁니다 (코드 식별자/파일 경로/`file:line`은 원문 유지).\n\n"
    "```json\n{schema}\n```\n"
)


FIX_DIRECTIVE = (
    "\n\n---\n\n"
    "당신은 daeyeon-bot의 **PR autofix 수정** 단계로 호출되었습니다."
    " 현재 작업 디렉터리는 해당 PR의 head 커밋이 체크아웃된 클론입니다.\n\n"
    "## 임무\n"
    "아래 '적용할 수정' 목록 **만** 구현합니다. 도구(Read/Edit/Write/Grep/Glob/"
    "Bash)로 실제 파일을 고치세요.\n\n"
    "## 규칙 (HARD)\n"
    "1. **목록에 없는 것은 건드리지 마세요.** 지나가다 발견한 다른 문제는"
    " 고치지 말고 최종 보고에 한 줄로만 적습니다. 범위를 넘긴 diff는"
    " 핸들러가 push를 거부합니다.\n"
    "2. 주변 코드의 관용구·명명·주석 밀도를 따르세요. 리포지터리에"
    " CLAUDE.md / CONTRIBUTING.md 가 있으면 먼저 읽으세요.\n"
    "3. `git commit`, `git push`, `git checkout`, 브랜치 조작을 하지 마세요."
    " 커밋과 push는 핸들러가 합니다. 작업 트리만 수정하면 됩니다.\n"
    "4. 다음 경로는 절대 수정하지 마세요: {protected}\n"
    "5. 어떤 항목을 구현할 수 없다고 판단되면, 억지로 고치지 말고 최종"
    " 보고에 그 항목과 이유를 적으세요.\n\n"
    "## 검증 (이 리포지터리의 방식대로)\n"
    "수정을 마쳤으면 **이 리포지터리가 원래 쓰는 방식으로** 스스로 검증하세요."
    " 검증 명령은 설정에 박혀 있지 않습니다 — 리포지터리를 읽고 직접 알아내야"
    " 합니다. 볼 곳: `CLAUDE.md` / `AGENTS.md` / `CONTRIBUTING.md`,"
    " `justfile` / `Makefile` / `tasks.py` / `package.json` scripts,"
    " 그리고 `.github/workflows/` 중 pull_request 트리거를 가진 것.\n\n"
    "고른 명령은 **내가 실제로 건드린 것에 대응**해야 합니다. 전체 CI를"
    " 재현하려 하지 마세요 — 대부분은 이 클론에서 돌지 않습니다"
    " (하드웨어 러너, 비어 있는 서브모듈, 없는 secret). Python 파일을"
    " 고쳤으면 그 파일의 lint/unit test, 설정을 고쳤으면 그 설정의 validator"
    " 정도가 적정선입니다.\n\n"
    "명령을 Bash로 **직접 실행해 보고**, 결과를 확인한 뒤 최종 보고에 적으세요."
    " 실패하면 원인이 내 수정 때문인지 보고, 내 탓이면 고치세요.\n\n"
    "### 명령 작성 규칙 (HARD)\n"
    "핸들러는 이 명령을 **현재 작업 디렉터리(= 이 리포지터리의 루트)에서**"
    " 그대로 다시 실행합니다. 따라서:\n"
    "- 경로는 **리포지터리 루트 기준 상대 경로**로 씁니다."
    " `ansible/roles/x/tasks/main.yaml` (O),"
    " `var/pr-autofix/<owner>__<repo>/ansible/...` (X — cwd가 이미 그 안입니다).\n"
    "- 워크스페이스 밖의 **절대 경로를 쓰지 마세요.** 특히 daeyeon-bot 자신의"
    " 디렉터리나 그 venv를 가리키면 안 됩니다 (`uv --project /home/.../daeyeon-bot`"
    " 같은 것). 이 리포지터리의 도구를 이 리포지터리 기준으로 쓰세요.\n"
    "- 한 줄로 끝나야 합니다. 여러 단계는 `&&` 로 이으세요.\n"
    "- 실행해서 통과한 명령만 적으세요. 돌려보지 않은 명령을 적으면"
    " 핸들러가 실패로 판정하고 push를 거부합니다.\n\n"
    "적정한 검증 방법을 못 찾겠으면 그렇다고 적으세요. 억지로 만들어내지"
    " 마세요 — 최종 판정은 어차피 이 PR의 CI가 합니다.\n\n"
    "## 최종 보고\n"
    "작업이 끝나면 마지막 메시지에 항목별로 한국어 한두 줄 요약을 씁니다:\n"
    "```\n"
    "- [comment #<id>] <무엇을 어떻게 바꿨는지> (<파일:라인>)\n"
    "```\n"
    "구현하지 못한 항목은 `- [comment #<id>] 미적용 — <이유>` 로 적습니다.\n\n"
    "그리고 마지막 줄에 검증 명령을 **정확히 이 형식으로** 한 줄 적습니다."
    " 핸들러가 이 줄을 파싱해서 같은 명령을 다시 돌리고, 그 exit code로"
    " push 여부를 정합니다:\n"
    "```\n"
    "VERIFY: <실행한 셸 명령 한 줄>\n"
    "```\n"
    "검증 방법을 못 찾았으면 `VERIFY: none` 이라고 적습니다."
    " 여러 명령이 필요하면 `&&` 로 이으세요. 이 줄은 반드시 있어야 합니다.\n"
)

# `VERIFY:` line the fix agent appends. Anchored to the start of a line so a
# mention inside prose or a code block cannot be mistaken for the directive.
_VERIFY_LINE_RE = re.compile(r"^\s*VERIFY:\s*(?P<cmd>.+?)\s*$", re.MULTILINE)
_VERIFY_NONE = frozenset({"none", "없음", "n/a", "-"})
# A verify command is run by the handler, so keep it to one plausible line.
_MAX_VERIFY_COMMAND_CHARS = 500


def parse_verify_command(agent_reply: str) -> str | None:
    """Extract the `VERIFY:` command the fix agent chose, or None.

    The agent picks the command because it can READ the repository — its
    conventions, its justfile, its pull_request workflows — while `config.toml`
    cannot and would go stale every time a repo changed its CI. The handler
    still runs the command itself and gates on the exit code, so the decision
    of WHAT to run is the model's and the judgement of whether it PASSED stays
    with real process exit status rather than an LLM's self-report.

    This does not widen the agent's reach: it already holds Bash inside the
    same workspace, so it could run this command itself either way.

    Takes the LAST match — an agent that revises its choice mid-message ends
    with the one it settled on.
    """
    matches = _VERIFY_LINE_RE.findall(agent_reply or "")
    if not matches:
        return None
    command = matches[-1].strip().strip("`").strip()
    if not command or command.lower() in _VERIFY_NONE:
        return None
    if len(command) > _MAX_VERIFY_COMMAND_CHARS:
        return None
    return command


def build_triage_system_prompt(persona_body: str) -> str:
    """Persona + the triage output contract."""
    schema = json.dumps(TriageOutput.model_json_schema(), ensure_ascii=False, indent=2)
    return persona_body + TRIAGE_DIRECTIVE.replace("{schema}", schema)


def build_fix_system_prompt(persona_body: str, *, protected_paths: list[str]) -> str:
    """Persona + the fix-agent contract, with the protected-path list inlined.

    The list is repeated in the prompt even though the handler enforces it
    afterwards from `git diff`: telling the agent up front avoids burning a
    whole run on work that will be rejected at the gate.
    """
    protected = ", ".join(f"`{p}`" for p in protected_paths) or "(없음)"
    return persona_body + FIX_DIRECTIVE.replace("{protected}", protected)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… (truncated, {len(text) - limit} more chars)"


def render_comment(comment: FeedbackComment) -> str:
    """One feedback item as markdown for the triage prompt."""
    lines = [f"### comment_id: {comment.comment_id}"]
    lines.append(f"- 작성자: `{comment.author}` ({comment.kind})")
    if comment.path:
        anchor = f"{comment.path}:{comment.line}" if comment.line else comment.path
        lines.append(f"- 위치: `{anchor}`")
    if comment.diff_hunk:
        lines.append("- 해당 diff hunk:")
        lines.append("```diff")
        lines.append(_truncate(comment.diff_hunk, _MAX_HUNK_CHARS))
        lines.append("```")
    lines.append("- 본문:")
    lines.append("```")
    lines.append(_truncate(comment.body, _MAX_COMMENT_CHARS))
    lines.append("```")
    return "\n".join(lines)


def render_triage_message(
    *,
    repo: str,
    pr_number: int,
    title: str,
    body: str,
    head_sha: str,
    files: list[dict[str, object]],
    comments: list[FeedbackComment],
) -> str:
    """User message for the triage stage: PR context + every pending comment.

    The changed-file patches come along because most rejection reasons — "봇이
    diff를 잘못 읽었다", "이미 처리됨", "범위 밖" — can only be established
    against the actual diff. A triage prompt without the diff can do no better
    than paraphrase the comment back.
    """
    parts = [
        f"# PR {repo}#{pr_number} @ `{head_sha[:12]}`",
        f"**제목**: {title}",
    ]
    if body.strip():
        parts.append("**본문**:\n" + _truncate(body.strip(), 3000))

    parts.append("\n## 변경된 파일")
    for raw in files:
        path = str(raw.get("filename", ""))
        status = str(raw.get("status", ""))
        adds, dels = raw.get("additions", 0), raw.get("deletions", 0)
        parts.append(f"\n### `{path}` ({status}, +{adds}/-{dels})")
        patch = raw.get("patch")
        if isinstance(patch, str) and patch:
            parts.append("```diff\n" + _truncate(patch, 6000) + "\n```")
        else:
            parts.append("_(patch 없음 — binary 또는 too large)_")

    parts.append(f"\n## 판정할 리뷰 코멘트 ({len(comments)}건)")
    parts.extend(render_comment(c) for c in comments)
    parts.append(
        f"\n---\n위 {len(comments)}건 각각에 대해 판정 결과를 JSON으로 출력하세요."
        " 모든 comment_id가 정확히 한 번씩 나와야 합니다."
    )
    return "\n\n".join(parts)


def render_fix_message(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    accepted: list[tuple[TriageDecision, FeedbackComment]],
) -> str:
    """User message for the fix agent: only the accepted items, with context."""
    parts = [
        f"# {repo}#{pr_number} @ `{head_sha[:12]}` — 적용할 수정 {len(accepted)}건",
        "작업 디렉터리가 이 PR의 head 커밋입니다. 아래 항목만 구현하세요.",
    ]
    for decision, comment in accepted:
        block = [f"\n## [comment #{decision.comment_id}] `{comment.author}`"]
        if comment.path:
            anchor = f"{comment.path}:{comment.line}" if comment.line else comment.path
            block.append(f"- 위치: `{anchor}`")
        block.append(f"- 리뷰어 지적:\n```\n{_truncate(comment.body, _MAX_COMMENT_CHARS)}\n```")
        block.append(f"- **적용할 수정**: {decision.fix_instruction}")
        if decision.evidence:
            block.append(f"- 근거: {decision.evidence}")
        parts.append("\n".join(block))
    return "\n".join(parts)


__all__ = [
    "FIX_DIRECTIVE",
    "TRIAGE_DIRECTIVE",
    "build_fix_system_prompt",
    "build_triage_system_prompt",
    "parse_verify_command",
    "render_comment",
    "render_fix_message",
    "render_triage_message",
]
