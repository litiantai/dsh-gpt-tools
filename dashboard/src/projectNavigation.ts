import { useSearchParams } from 'react-router-dom';

export const projectSections = {
  intelligence: { label: 'AI 对话', tabs: ['chat','drafts','competitors','collaboration','intelligence_settings'] },
  overview: { label: '工作概览', tabs: ['monitoring'] },
  requirements: { label: '需求池', tabs: ['requirements', 'signals'] },
  tasks: { label: '任务流水线', tabs: ['runs', 'sessions', 'reviews', 'events'] },
  evidence: { label: '巡查与证据', tabs: ['inspections', 'timeline', 'evaluations', 'daily'] },
  releases: { label: '代码交付与安装', tabs: ['deliveries', 'code_reviews', 'release_prs', 'repair_issues', 'releases'] },
  settings: { label: '项目设置', tabs: ['roles', 'git', 'policy', 'environment'] },
} satisfies Record<string, { label: string; tabs: string[] }>;
export type ProjectSection = keyof typeof projectSections;

export function projectSection(params: URLSearchParams): ProjectSection {
  const value = params.get('view') || 'overview';
  return Object.hasOwn(projectSections, value) ? value as ProjectSection : 'overview';
}

export function useProjectNavigation() {
  const [params, setParams] = useSearchParams();
  const section = projectSection(params);
  const validTab = (key: ProjectSection, value: string | null) =>
    value && projectSections[key].tabs.includes(value) ? value : projectSections[key].tabs[0];
  const tab = validTab(section, params.get('tab') || params.get(`${section}Tab`));
  const navigate = (key: ProjectSection, tabKey?: string) => setParams(previous => {
    const next = new URLSearchParams(previous);
    next.set(`${section}Tab`, tab);
    const target = validTab(key, tabKey || next.get(`${key}Tab`));
    next.set('view', key);
    next.set('tab', target);
    next.set(`${key}Tab`, target);
    return next;
  });
  const setTab = (key: string) => {
    const target = (Object.keys(projectSections) as ProjectSection[]).find(s => projectSections[s].tabs.includes(key));
    if (target) navigate(target, key);
  };
  return { section, tab, navigate, setTab };
}
