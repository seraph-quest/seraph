import { useCallback } from "react";
import { useChatStore } from "../stores/chatStore";
import { getIdleState, getThinkingState } from "../lib/animationStateMachine";

export function useAgentAnimation() {
  const { agentVisual, setAgentVisual, resetAgentVisual } = useChatStore();

  const onThinking = useCallback(() => {
    const thinking = getThinkingState();
    setAgentVisual({
      animationState: "thinking",
      positionX: thinking.positionX,
      speechText: null,
    });
  }, [setAgentVisual]);

  const onToolDetected = useCallback(
    (_toolName: string, stepContent?: string) => {
      setAgentVisual({
        animationState: "casting",
        speechText: stepContent ?? null,
      });
    },
    [setAgentVisual]
  );

  const onFinalAnswer = useCallback(
    (answer: string) => {
      const idle = getIdleState();
      const truncated =
        answer.length > 80 ? answer.slice(0, 80) + "..." : answer;

      setAgentVisual({
        animationState: "speaking",
        positionX: idle.positionX,
        speechText: truncated,
      });
    },
    [setAgentVisual]
  );

  const returnToIdle = useCallback(() => {
    resetAgentVisual();
  }, [resetAgentVisual]);

  return {
    currentPosition: agentVisual.positionX,
    facingDirection: agentVisual.facing,
    animationState: agentVisual.animationState,
    onThinking,
    onToolDetected,
    onFinalAnswer,
    returnToIdle,
  };
}
