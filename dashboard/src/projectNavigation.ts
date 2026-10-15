import { useCallback, useRef } from 'react';
import { useSearchParams } from 'react-router-dom';

export const projectSections = {
  intelligence: { label: 'AI 对话', tabs: ['chat','drafts','collaboration','intelligence_settings'] },
  competitors: { label: '竞品分析', tabs: ['competitors'] },
  overview: { label: '工作概览', tabs: ['monitoring'] },
  requirements: { label: '需求池', tabs: ['requirements', 'signals'] },
  tasks: { label: '任务流水线', tabs: ['runs', 'requirement_groups', 'coordinator', 'sessions', 'reviews', 'events'] },
  evidence: { label: '巡查与证据', tabs: ['inspections', 'inspection_exceptions', 'inspection_routes', 'timeline', 'evaluations', 'daily'] },
  releases: { label: '代码交付与安装', tabs: ['deliveries', 'code_reviews', 'release_prs', 'repair_issues', 'releases'] },
  testing: { label: '测试链路', tabs: ['test_chains', 'test_chain_runs', 'test_chain_settings'] },
  settings: { label: '项目设置', tabs: ['roles', 'linked_projects', 'coordination_settings', 'research', 'git', 'policy', 'environment', 'notifications'] },
} satisfies Record<string, { label: string; tabs: string[] }>;
export type ProjectSection = keyof typeof projectSections;

export function projectSection(params: URLSearchParams): ProjectSection {
  if (params.get('view') === 'intelligence' && params.get('tab') === 'competitors') return 'competitors';
  const value = params.get('view') || 'overview';
  return Object.hasOwn(projectSections, value) ? value as ProjectSection : 'overview';
}

function firstTab(key: ProjectSection): string {
  return projectSections[key].tabs[0];
}

function validTab(key: ProjectSection, value: string | null | undefined): string {
  return value && projectSections[key].tabs.includes(value) ? value : firstTab(key);
}

// 解析顺序固定：本 section 的有效记忆 → 全局 tab（仅当对本 section 合法）→ 该 section 首 tab。
// 全局 tab 不再优先于 section 记忆，避免跨 section 的 tab 污染把内层页签回退到首个。
function resolveTab(key: ProjectSection, params: URLSearchParams): string {
  const remembered = params.get(`${key}Tab`);
  if (remembered && projectSections[key].tabs.includes(remembered)) return remembered;
  const current = params.get('tab');
  if (current && projectSections[key].tabs.includes(current)) return current;
  return firstTab(key);
}

export function useProjectNavigation() {
  const [params, setParams] = useSearchParams();
  // 保存最新 URL。setSearchParams 的函数式更新器只拿到本次渲染闭包的 searchParams
  // （react-router useSearchParams 不会链式累加），因此同一 tick 的重复导航必须依赖
  // 这个 ref 才能读到上一次已写出的参数。
  const latest = useRef<URLSearchParams>(params);
  latest.current = params;
  const section = projectSection(params);
  const tab = resolveTab(section, params);
  const navigate = useCallback((key: ProjectSection, tabKey?: string) => {
    setParams(previous => {
      // 写入路径只依据 URL 现状计算，不引用渲染闭包的 section/tab，避免陈旧参数覆盖记忆。
      const base = new URLSearchParams(latest.current ? latest.current.toString() : previous.toString());
      const from = projectSection(base);
      const currentTab = resolveTab(from, base);
      base.set(`${from}Tab`, currentTab);
      const target = validTab(key, tabKey || base.get(`${key}Tab`));
      base.set('view', key);
      base.set('tab', target);
      base.set(`${key}Tab`, target);
      // 立即同步 ref，使 onTabClick/onChange 双通道的重复调用幂等。
      latest.current = base;
      return base;
    });
  }, [setParams]);
  const setTab = useCallback((key: string) => {
    const target = (Object.keys(projectSections) as ProjectSection[]).find(s => projectSections[s].tabs.includes(key));
    if (target) navigate(target, key);
  }, [navigate]);
  return { section, tab, navigate, setTab };
}
