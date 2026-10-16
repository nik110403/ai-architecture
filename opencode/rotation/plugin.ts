import { createModelRotate } from "./model-rotate"

// OpenCode plugin entry for model rotation (opt-in; see README.md).
export default async function plugin(input: any) {
  return createModelRotate(input)
}
