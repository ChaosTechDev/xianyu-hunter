import { http } from '@/lib/http'

export interface PromptContent {
  filename: string
  content: string
}

export async function listPrompts(): Promise<string[]> {
  return await http('/api/prompts')
}

export async function getPromptContent(filename: string): Promise<PromptContent> {
  return await http(`/api/prompts/${filename}`)
}

export async function updatePrompt(filename: string, content: string): Promise<{ message: string }> {
  return await http(`/api/prompts/${filename}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })
}
