import { ref, onScopeDispose } from 'vue'
import { wsService } from '@/services/websocket'

// Global state for connection status
const isConnected = ref(wsService.isConnected)

// Update global state on events
wsService.on('connected', () => { isConnected.value = true })
wsService.on('disconnected', () => { isConnected.value = false })

export function useWebSocket() {
  /**
   * Register a callback for a specific WebSocket event.
   * Automatically removes the listener when the component is unmounted or scope disposed.
   * 
   * @param event The event name (e.g., 'tasks_updated')
   * @param callback The function to call when the event is received
   */
  function on(event: string, callback: (data: any) => void) {
    wsService.on(event, callback)
    
    // Clean up when the scope (component) is disposed
    onScopeDispose(() => {
      wsService.off(event, callback)
    })
  }

  return {
    isConnected,
    on,
  }
}
