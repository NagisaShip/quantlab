import { Heading } from '../components/ui'
import MentorCourse from './home/MentorCourse'
import type { PageProps } from './types'

/** 独立的研究导师页面：从研究流程、首页和后续深链都可以进入同一套课程。 */
export default function MentorPage({ notify, onNavigate, features }: PageProps) {
  return (
    <>
      <Heading
        eyebrow="RESEARCH MENTOR"
        title="研究导师"
        description="把研究方法拆成可练习、可检查的课程：假设、数据时点、信号因果、回测与基准、稳健性与多重检验、永续风险、执行与上线。"
      />
      <MentorCourse notify={notify} onNavigate={onNavigate} features={features} />
    </>
  )
}
