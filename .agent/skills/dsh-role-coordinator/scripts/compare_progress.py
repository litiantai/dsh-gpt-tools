"""比较控制器提供的真实检查，不接受模型自报完成度。可通过 JSON stdin 独立调用。"""
import json
import sys


def compare(before, after):
    old = before.get('checks', {})
    new = after.get('checks', {})
    fresh = bool(after.get('receipt_id')) and before.get('receipt_id') != after['receipt_id']
    resolved = sorted(k for k in old if old[k] in ('fail', 'blocked') and new.get(k) == 'pass') if fresh else []
    regressions = sorted(k for k in new if new[k] in ('fail', 'blocked') and (old.get(k) == 'pass' or k not in old)) if fresh else []
    missing = sorted(set(old) - set(new)) if fresh else []
    remaining = sorted(set(k for k in new if new[k] != 'pass') | set(missing))
    reviewed = bool(after.get('review_id')) and before.get('review_id') != after['review_id'] and 'issues' in before
    fixed_issues = sorted(set(before.get('issues', [])) - set(after.get('issues', []))) if reviewed else []
    new_issues = sorted(set(after.get('issues', [])) - set(before.get('issues', []))) if reviewed else []
    return {'improved': bool(resolved or fixed_issues) and not (regressions or new_issues or missing),
            'resolved_issues': fixed_issues, 'new_issues': new_issues, 'remaining_issues': after.get('issues', []), 'resolved': resolved,
            'remaining': remaining, 'regressions': regressions, 'missing_checks': missing, 'fresh_evidence': fresh or reviewed}


if __name__ == '__main__':
    data = json.load(sys.stdin)
    print(json.dumps(compare(data['before'], data['after']), ensure_ascii=False))
