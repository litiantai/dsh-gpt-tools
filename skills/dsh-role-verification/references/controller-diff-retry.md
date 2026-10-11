重要更正：控制器已提供非空且可读的 verification-diff.patch 与 verification-facts.json，git 元数据不可读（EPERM/permission denied）不得作为 blocked 理由；必须基于这份自包含差异与源码摘要完成独立判断。若仍返回 blocked，必须明确指出缺失的非 git 前提。
