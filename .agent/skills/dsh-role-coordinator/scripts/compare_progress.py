"""比较控制器提供的真实检查，不接受模型自报完成度。可通过 JSON stdin 独立调用。"""
import json
import sys


def compare(before, after):
    old = before.get('checks', {})
    new = after.get('checks', {})
    fresh = bool(after.get('receipt_id')) and before.get('receipt_id') != after['receipt_id']
    resolved = sorted(k for k in old if old[k] in ('fail', 'blocked') and new.get(k) == 'pass') if fresh else []
    regressions = sorted(k for k in new if new[k] in ('fail', 'blocked') and old.get(k) == 'pass') if fresh else []
    missing = sorted(set(old) - set(new)) if fresh else []
    remaining = sorted(set(k for k in new if new[k] != 'pass') | set(missing))
    reviewed = bool(after.get('review_id')) and before.get('review_id') != after['review_id'] and 'issues' in before
    fixed_issues = sorted(set(before.get('issues', [])) - set(after.get('issues', []))) if reviewed else []
    new_issues = sorted(set(after.get('issues', [])) - set(before.get('issues', []))) if reviewed else []
    resolved_tests = sorted(set(before.get('failed_tests', [])) & set(after.get('passed_tests', []))) if fresh else []
    new_tests = sorted(set(after.get('failed_tests', [])) - set(before.get('failed_tests', []))) if fresh else []
    fresh = fresh and bool(new) and after.get('retryable') is not True and after.get('failure_kind') not in ('dependency_network','agent_execution','verification_evidence')
    return {'resolved_tests':resolved_tests, 'new_failed_tests':new_tests, 'improved': bool(resolved or fixed_issues or resolved_tests) and not (regressions or new_issues or missing),
            'resolved_issues': fixed_issues, 'new_issues': new_issues, 'remaining_issues': after.get('issues', []), 'resolved': resolved,
            'remaining': remaining, 'regressions': regressions, 'missing_checks': missing, 'fresh_evidence': fresh or reviewed}


if __name__ == '__main__':
    data = json.load(sys.stdin)
    print(json.dumps(compare(data['before'], data['after']), ensure_ascii=False))
