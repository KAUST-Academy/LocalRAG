"""Download the 38 GitLab handbook pages used in Day2_RAG_Pipeline.ipynb into sample_docs/gitlab_handbook/.

Same pages, same pinned commit as the lab, saved as .md files with a small front matter (title + source URL),
so the app can cite real links. Standard library only:  python tools/fetch_sample_docs.py
"""
import concurrent.futures as cf
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://gitlab.com/api/v4/projects/gitlab-com%2Fcontent-sites%2Fhandbook/repository"
REF = "df66e66b937d38c1ed4e3dd452927ddf01be58b0"      # the lab's fixed commit, so everyone sees the same text
PAGES = """
people-group/time-off-and-absence/time-off-types.md
people-group/time-off-and-absence/leave-types.md
people-group/anti-harassment.md
people-group/contracts-probation-periods.md
people-group/acceptable-use-policy.md
people-group/general-onboarding/_index.md
people-group/offboarding/faq.md
people-policies/inc-usa.md
people-policies/leave-of-absence/us.md
people-policies/ireland-ltd/right-to-disconnect-policy.md
people-policies/france-sas/remote-work-charter.md
total-rewards/benefits/_index.md
total-rewards/benefits/parental-leave-toolkit.md
total-rewards/benefits/general-and-entity-benefits/ltd-benefits-uk.md
total-rewards/benefits/general-and-entity-benefits/gitlab-ireland-ltd.md
total-rewards/benefits/general-and-entity-benefits/bv-benefits-netherlands.md
total-rewards/benefits/general-and-entity-benefits/inc-benefits-us/_index.md
total-rewards/benefits/general-and-entity-benefits/pty-benefits-australia.md
total-rewards/benefits/general-and-entity-benefits/canada-corp-benefits.md
total-rewards/compensation/_index.md
total-rewards/stock-options.md
finance/expenses.md
finance/spending-company-money.md
finance/procurement/individual-use-software.md
finance/accounts-payable/corp-credit-cards.md
security/policies_and_standards/password-standard.md
security/policies_and_standards/data-classification-standard.md
security/policies_and_standards/physical-security-standard-for-company-assets.md
security/policies_and_standards/records-retention-deletion.md
security/policies_and_standards/token-management-standard.md
legal/anti-corruption-policy.md
legal/anti-fraud-policy.md
legal/anti-retaliation-policy.md
legal/policies/gifts-contributions.md
legal/acceptable-use-policy.md
business-technology/_index.md
communication/confidentiality-levels.md
hiring/referral-process.md
""".split()

OUT = Path(__file__).resolve().parent.parent / "sample_docs" / "gitlab_handbook"


def fetch(page):
    path = "content/handbook/" + page
    url = f"{API}/files/{urllib.parse.quote(path, safe='')}/raw?ref={REF}"
    text = urllib.request.urlopen(url, timeout=60).read().decode("utf-8")
    front, body = text.split("\n---\n", 1)                                     # YAML front matter | markdown body
    title = re.search(r'^title:\s*"?(.*?)"?\s*$', front, re.M).group(1)
    source = "https://handbook.gitlab.com/handbook/" + page.replace("/_index.md", "/").replace(".md", "/")
    return page, title, source, body


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with cf.ThreadPoolExecutor(8) as pool:
        pages = list(pool.map(fetch, PAGES))
    used = set()
    for page, title, source, body in pages:
        name = re.sub(r'[<>:"/\\|?*]', "-", title).strip(" .")              # a title is not always a legal Windows file name
        if name.lower() in used:                                              # two pages are both "Acceptable Use Policy"
            name = f"{name} ({page.split('/')[0]})"
        used.add(name.lower())
        front = f"---\ntitle: {json.dumps(title)}\nsource: {source}\n---\n"
        (OUT / f"{name}.md").write_text(front + body, encoding="utf-8", newline="\n")    # keep the repo's \n line ends
    (OUT.parent / "ATTRIBUTION.txt").write_text(                       # outside the folder, so it is not indexed
        "These 38 pages are from the GitLab Handbook (https://handbook.gitlab.com), "
        f"repository gitlab-com/content-sites/handbook at commit {REF}.\n"
        "Text (c) GitLab Inc., licensed CC BY-SA 4.0: https://creativecommons.org/licenses/by-sa/4.0/\n", encoding="utf-8")
    print(len(pages), "pages,", sum(len(p[3]) for p in pages), "characters ->", OUT)


if __name__ == "__main__":
    main()
