export type Role = 'ADMIN' | 'USER'
export type Visibility = 'PUBLIC' | 'PRIVATE' | 'TEAM'
export type ModerationStatus = 'APPROVED' | 'PENDING' | 'REJECTED'
export type ViewKey = 'public' | 'personal' | 'team' | 'admin'

export interface UserProfile {
  id: number
  name: string
  email: string
  role: Role
}

export interface AuthResponse {
  token: string
  user: UserProfile
}

export interface ImageAsset {
  id: number
  title: string
  description: string
  category: string
  tags: string[]
  imageUrl: string
  thumbnailUrl: string
  visibility: Visibility
  moderationStatus: ModerationStatus
  teamId: number | null
  teamName: string | null
  uploader: UserProfile
  createdAt: string
}

export interface TeamMember {
  id: number
  name: string
  email: string
  teamRole: 'OWNER' | 'MEMBER'
}

export interface TeamSummary {
  id: number
  name: string
  description: string
  memberCount: number
  members: TeamMember[]
}

export interface DashboardStats {
  userCount: number
  imageCount: number
  publicImageCount: number
  pendingModerationCount: number
  teamCount: number
}

export interface TeamEvent {
  type: string
  message: string
  imageId: number | null
  teamId: number | null
  actorName: string | null
  occurredAt: string
}
