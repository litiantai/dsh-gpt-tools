"""从平台回执抽取验证与审查证据；不执行网络请求、不修改状态。"""
import json
import sys


def collect(record):
    snapshot = {'receipt_id': None, 'checks': {}}
    for receipt in reversed(record.get('receipts', [])):
        if receipt.get('action') != 'verify':
            continue
        snapshot['receipt_id'] = receipt.get('call_id')
        result = receipt.get('result') or {}
        snapshot.update(failed_tests=result.get('failed_test_ids', []), passed_tests=result.get('passed_test_ids', []),
                        failure_kind=result.get('failure_kind'), retryable=result.get('retryable'),
                        verification_identity=receipt.get('verification_identity'))
        for check in result.get('checks', []):
            if isinstance(check, dict) and check.get('required',True) and (check.get('name') or check.get('id')):
                snapshot['checks'][str(check.get('id') or check['name'])] = check.get('status', 'unknown')
        break
    review = record.get('last_review_result') or {}
    if review.get('review_id') and review.get('decision') in ('revise', 'done', 'approve'):
        snapshot['review_id'] = review['review_id']
        snapshot['issues'] = sorted(json.dumps(issue, sort_keys=True, ensure_ascii=False) for issue in review.get('issues', []))
    return snapshot


if __name__ == '__main__':
    print(json.dumps(collect(json.load(sys.stdin)), ensure_ascii=False))
