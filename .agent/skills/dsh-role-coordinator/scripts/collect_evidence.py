"""从平台回执抽取验证与审查证据；不执行网络请求、不修改状态。"""
import json
import sys


def collect(record):
    snapshot = {'receipt_id': None, 'checks': {}}
    for receipt in reversed(record.get('receipts', [])):
        if receipt.get('action') != 'verify':
            continue
        snapshot['receipt_id'] = receipt.get('call_id')
        for check in (receipt.get('result') or {}).get('checks', []):
            if isinstance(check, dict) and (check.get('name') or check.get('id')):
                snapshot['checks'][str(check.get('id') or check['name'])] = check.get('status', 'unknown')
        break
    review = record.get('last_review_result') or {}
    if review.get('review_id') and review.get('decision') in ('revise', 'done', 'approve'):
        snapshot['review_id'] = review['review_id']
        snapshot['issues'] = sorted(json.dumps(issue, sort_keys=True, ensure_ascii=False) for issue in review.get('issues', []))
    return snapshot


if __name__ == '__main__':
    print(json.dumps(collect(json.load(sys.stdin)), ensure_ascii=False))
