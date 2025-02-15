/*
 * Copyright (C) 2022 Apple, Inc. All Rights Reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY APPLE INC. ``AS IS'' AND ANY
 * EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
 * PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL APPLE INC. OR
 * CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
 * PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
 * PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
 * OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#include "config.h"
#include "LoadableImportMap.h"

#include "DefaultResourceLoadPriority.h"
#include "Element.h"
#include "FetchIdioms.h"
#include "ScriptElement.h"
#include "ScriptSourceCode.h"
#include "SubresourceIntegrity.h"
#include <wtf/NeverDestroyed.h>
#include <wtf/text/StringImpl.h>

namespace WebCore {

Ref<LoadableImportMap> LoadableImportMap::create(const AtomString& nonce, const AtomString& integrityMetadata, ReferrerPolicy policy, const AtomString& crossOriginMode, const AtomString& initiatorType, bool isInUserAgentShadowTree, bool isAsync)
{
    return adoptRef(*new LoadableImportMap(nonce, integrityMetadata, policy, crossOriginMode, initiatorType, isInUserAgentShadowTree, isAsync));
}

LoadableImportMap::LoadableImportMap(const AtomString& nonce, const AtomString& integrity, ReferrerPolicy policy, const AtomString& crossOriginMode, const AtomString& initiatorType, bool isInUserAgentShadowTree, bool isAsync)
    : LoadableNonModuleScriptBase(nonce, integrity, policy, RequestPriority::Auto, crossOriginMode, "utf-8"_s, initiatorType, isInUserAgentShadowTree, isAsync)
{
}

void LoadableImportMap::execute(ScriptElement& scriptElement)
{
    ASSERT(!m_error);
    scriptElement.registerImportMap(ScriptSourceCode(m_cachedScript.get(), JSC::SourceProviderSourceType::ImportMap, *this));
}

}
