"""Compact cart, wallet, order aftercare and customer notification controls."""
import asyncio
from html import escape
from io import BytesIO
from decimal import Decimal, ROUND_HALF_UP

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton as Button, InlineKeyboardMarkup, BufferedInputFile

from config import ADMIN_IDS
from services import cart, customer_tools as tools
from services import notification_inbox as inbox
from utils.ui import answer_card, show, update_card, track_card

router = Router()


class CustomerInput(StatesGroup):
    quantity = State()
    cancel_reason = State()
    claim_reply = State()
    admin_reply = State()
    term_days = State()
    hold_resolution = State()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def home():
    return kb([[Button(text='🏠 Home',callback_data='main_menu')]])


def _cart_display_amount(value):
    """Short display only; database and checkout amounts stay full precision."""
    amount = Decimal(str(value or 0))
    rounded = amount.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP)
    if amount and rounded == 0:
        return f'{amount:.8f}'.rstrip('0').rstrip('.')
    return f'{rounded:.3f}'.rstrip('0').rstrip('.') or '0'


async def notifications_screen(target,page=0):
    data=await asyncio.to_thread(inbox.listing,target.from_user.id,page,target.bot.id)
    text=("🔔 <b>RAINDEALS NOTIFICATIONS</b>\n\n"
          "✨ Stay informed about your RainDeals account.\n"
          "💳 Payment and wallet updates, support replies, claim decisions, and restock alerts keep you up to date.\n"
          "📩 Tap an update below to open its related action.\n"
          "⚙️ Manage optional alerts in Preferences below.\n\n")
    buttons=[]
    for row in data['rows']:
        marker='🔴' if row['unread'] else '✓'
        text+=(f"\n{marker} <b>{escape(row['title'])}</b>\n"
               f"{escape(row['summary'])}\n"
               f"<i>{row['created_at']:%d %b %H:%M} UTC</i>\n")
        buttons.append([Button(text=f"{marker} {row['title'][:50]}",
                               callback_data=f"ct_notice_{row['id']}_{data['page']}")])
    if not data['rows']:
        text+='\n✨ You’re all caught up. New updates will appear here.'
    pagination=[]
    if data['page']:
        pagination.append(Button(text='‹ Previous',callback_data=f"ct_inbox_{data['page']-1}"))
    if data['has_more']:
        pagination.append(Button(text='Next ›',callback_data=f"ct_inbox_{data['page']+1}"))
    if pagination:
        buttons.append(pagination)
    if data['unread']:
        buttons.append([Button(text='✓ Mark all as read',callback_data=f"ct_readall_{data['newest_id']}")])
    buttons.extend([
        [Button(text='⚙ Preferences',callback_data='ct_preferences')],
        [Button(text='🏠 Main Menu',callback_data='main_menu')],
    ])
    await answer_card(target,text,parse_mode='HTML',reply_markup=kb(buttons),replace_media=True)


async def cart_screen(target,page=0,notice=''):
    lines,busy = await asyncio.to_thread(cart.view,target.from_user.id)
    page=max(0,min(page,max(0,(len(lines)-1)//6)))
    text='🛒 <b>Your cart</b>\n'+(escape(notice)+'\n' if notice else '')
    text+='\nItems are not reserved. Prices and stock are checked at checkout.\n'
    buttons=[]
    from decimal import Decimal
    for line in lines[page*6:page*6+6]:
        text+=f"\n<b>{escape(line['name'][:80])}</b>\n{line['quantity']} × · ${_cart_display_amount(line['total'])}"
        if line['error']:
            text+=' · '+escape(line['error'])
        buttons.append([Button(text=f"✏ Qty · {line['quantity']}",callback_data=f"ct_qty_{line['product_id']}"),
                        Button(text='✕ Remove',callback_data=f"ct_remove_{line['product_id']}")])
    text+=f"\n\nTotal: <b>${_cart_display_amount(sum((Decimal(x['total']) for x in lines),Decimal(0)))}</b>"
    if not lines:
        text+='\nYour cart is empty.'
    if busy:
        buttons.append([Button(text='View checkout status',callback_data=f'ct_result_{busy}')])
    elif lines:
        buttons.append([Button(text='Review checkout',callback_data='ct_review',style='success'),Button(text='Clear cart',callback_data='ct_clear')])
    if len(lines)>6:
        buttons.append([Button(text='‹ Previous',callback_data=f'ct_page_{max(0,page-1)}'),Button(text='Next ›',callback_data=f'ct_page_{page+1}')])
    buttons.append([Button(text='🛍 Shop',callback_data='products_menu'),Button(text='🏠 Home',callback_data='main_menu')])
    await answer_card(target,text,parse_mode='HTML',reply_markup=kb(buttons),replace_media=True)


async def checkout_result(target,token):
    data=await asyncio.to_thread(cart.result,target.from_user.id,token)
    text=f"🛒 <b>Checkout · {escape(data['status'])}</b>\n"
    buttons=[]
    for row in data['results']:
        text+='\n'+escape(row['name'][:60])+': '
        if row['order_id']:
            text+=f"order #{row['order_id']}"
            buttons.append([Button(text=f"Order #{row['order_id']}",callback_data=f"order_detail_{row['order_id']}")])
        else:
            text+=escape(row['error'] or row['status'])
    text+='\n\nEach successful item creates its own order and wallet charge. Failed or unprocessed items remain in the cart.'
    if data['status']=='held':
        text+='\nA supplier result needs staff review. Do not buy it again.'
    buttons.append([Button(text='Refresh',callback_data=f'ct_result_{token}'),Button(text='Cart',callback_data='ct_cart')])
    buttons.append([Button(text='Home',callback_data='main_menu')])
    await answer_card(target,text,parse_mode='HTML',reply_markup=kb(buttons),replace_media=True)


async def thread_screen(target,order_id):
    admin=target.from_user.id in ADMIN_IDS
    status,rows=await asyncio.to_thread(tools.claim_conversation,target.from_user.id,order_id,admin)
    text=f'💬 <b>Order #{order_id} · Claim conversation</b>\nStatus: {escape(status)}\n'
    buttons=[]
    for row in rows[-8:]:
        text+=f"\n<b>{row['role'].title()} · {row['date']:%d %b %H:%M} UTC</b>\n{escape(row['text'][:250])}\n"
        if row['photo']:
            buttons.append([Button(text=f"📷 Evidence #{row['id']}",callback_data=f"ct_proof_{row['id']}")])
    if admin and status in ('submitted','needs_info'):
        buttons.append([Button(text='Ask for information',callback_data=f'ct_ask_{order_id}',style='primary')])
    if status=='needs_info':
        buttons.append([Button(text='Reply / add proof',callback_data=f'ct_reply_{order_id}',style='primary')])
    buttons.append([Button(text='⬅ Claim',callback_data=f'warranty_admin_{order_id}' if admin else f'warranty_order_{order_id}')])
    await answer_card(target,text,parse_mode='HTML',reply_markup=kb(buttons),replace_media=True)


@router.callback_query(F.data.startswith('ct_'))
async def customer_action(callback:CallbackQuery,state:FSMContext):
    await callback.answer()
    parts=callback.data.split('_',2)
    action=parts[1]
    value=parts[2] if len(parts)>2 else ''
    user_id=callback.from_user.id
    try:
        if action in ('cart','page','add','remove','clear'):
            notice=''
            if action=='add':
                data=await state.get_data()
                await asyncio.to_thread(cart.change,user_id,int(value),int(data.get(f'qty_{value}',1)))
                notice='Added to cart.'
            elif action=='remove':
                await asyncio.to_thread(cart.change,user_id,int(value),0)
            elif action=='clear':
                await asyncio.to_thread(cart.clear,user_id)
            await state.clear()
            await cart_screen(callback,int(value) if action=='page' else 0,notice)
        elif action in ('qty','pick'):
            await state.clear()
            await state.set_state(CustomerInput.quantity)
            await state.update_data(customer_product=int(value))
            prompt=('🛒 Add to cart\n\nSend the quantity to keep in your cart (1–1,000). '
                    'If this product is already in the cart, this replaces its quantity. No payment is taken.'
                    if action=='pick' else 'Send the quantity (1–1,000), or 0 to remove.')
            await show(callback,prompt,state=state,reply_markup=kb([[Button(text='Cancel',callback_data='ct_cart'),Button(text='Product',callback_data=f'product_{int(value)}')]]))
        elif action=='review':
            token,lines,total=await asyncio.to_thread(cart.prepare,user_id)
            text='🧾 <b>Review checkout</b>\n\n'+'\n'.join(f"{escape(x['name'][:60])} × {x['quantity']} · ${_cart_display_amount(x['total'])}" for x in lines)
            text+=f'\n\n<b>Total: ${_cart_display_amount(total)} USDT</b>\nReview expires in 5 minutes. Items process separately; successful orders remain purchased if another item fails.'
            await show(callback,text,parse_mode='HTML',reply_markup=kb([[Button(text='Pay from wallet',callback_data=f'ct_pay_{token}',style='success'),Button(text='Back',callback_data='ct_cart')]]))
        elif action=='pay':
            # Business work stays alive when navigation changes; stale rendering alone is discarded.
            await show(callback,'🛒 Processing your cart. Orders will appear in My Orders.',reply_markup=home())
            await cart.checkout(user_id,value,callback.bot.id)
            await checkout_result(callback,value)
        elif action=='result':
            await checkout_result(callback,value)
        elif action=='inbox':
            await state.clear()
            await notifications_screen(callback,max(0,int(value or '0')))
        elif action=='notice':
            job_id,page=(value.split('_',1)+['0'])[:2] if '_' in value else (value,'0')
            row=await asyncio.to_thread(inbox.read,user_id,int(job_id),callback.bot.id)
            buttons=[]
            if row['claim'] and row['order_id']:
                label='Open refund request' if row['claim']['request_type']=='refund' else 'Open warranty request'
                buttons.append([Button(text=label,callback_data=f"warranty_order_{row['order_id']}")])
            elif row['kind']=='deposit':
                buttons.append([Button(text='Open Wallet',callback_data='deposit_start')])
            elif row['kind']=='support':
                buttons.append([Button(text='Open Support',callback_data='support_ticket')])
            elif row['kind']=='wallet':
                buttons.append([Button(text='Open wallet',callback_data='deposit_start')])
            elif row['kind']=='restock':
                buttons.append([Button(text='Open Shop',callback_data='products_menu')])
            elif row['kind']=='order' and row.get('order') and row['order'].get('status')=='pending_info':
                buttons.append([Button(text='🧾 Submit required information',callback_data=f"order_submit_info_{row['order_id']}")])
            elif row['kind'] in ('order','subscription'):
                buttons.append([Button(text='My Orders',callback_data='orders_menu')])
            buttons.append([Button(text='⬅ Notifications',callback_data=f'ct_inbox_{max(0,int(page))}'),Button(text='Home',callback_data='main_menu')])
            detail=(f"🔔 <b>{escape(row['title'])}</b>\n<i>{row['created_at']:%d %b %Y %H:%M} UTC</i>\n\n"
                    f"{escape(row.get('content') or row['summary'])}")
            await show(callback,detail,parse_mode='HTML',reply_markup=kb(buttons),replace_media=True)
        elif action=='readall':
            await asyncio.to_thread(inbox.mark_read,user_id,int(value),callback.bot.id)
            await notifications_screen(callback)
        elif action in ('preferences','toggle'):
            await state.clear()
            prefs=await asyncio.to_thread(tools.preferences,user_id,value if action=='toggle' else None)
            buttons=[[Button(text=f"{'✅' if enabled else '◻'} {label}",callback_data=f'ct_toggle_{key}')]
                     for key,label in [('restock','Restock alerts'),('order_progress','Order progress'),('subscription','Subscription reminders')] for enabled in [prefs[key]]]
            buttons+=[[Button(text='⬅ Notifications',callback_data='ct_inbox'),Button(text='Home',callback_data='main_menu')]]
            await show(callback,'⚙ <b>Notification preferences</b>\n\nChoose optional updates for your inbox. Payment receipts, completed deliveries and security messages remain enabled.',parse_mode='HTML',reply_markup=kb(buttons))
        elif action=='wallet':
            page=max(0,int(value))
            balance,rows,more=await asyncio.to_thread(tools.wallet_statement,user_id,page)
            text=f'🧾 <b>Wallet statement</b>\nCurrent balance: ${balance:.8f}\n'
            for row in rows:
                text+=f"\n{row['date']:%d %b %H:%M} UTC · {escape(row['label'])}\n{row['amount']:+.8f} → ${row['balance']:.8f}\n"
            text+='\nHistory begins when wallet tracking is enabled. Changes made by other services appear as explicit reconciliation entries.'
            buttons=[]
            if page: buttons.append(Button(text='‹ Previous',callback_data=f'ct_wallet_{page-1}'))
            if more: buttons.append(Button(text='Next ›',callback_data=f'ct_wallet_{page+1}'))
            await show(callback,text,parse_mode='HTML',reply_markup=kb(([buttons] if buttons else [])+[[Button(text='Wallet',callback_data='deposit_start'),Button(text='Home',callback_data='main_menu')]]))
        elif action=='received':
            when=await asyncio.to_thread(tools.receive_order,user_id,int(value))
            await show(callback,f'✅ Receipt confirmed for order #{int(value)}.',reply_markup=kb([[Button(text='Back to order',callback_data=f'order_detail_{int(value)}')]]))
        elif action=='issue':
            from services.warranty import context
            ctx=await asyncio.to_thread(context,int(value),user_id)
            claim=bool(ctx and ctx['policy']!='skip')
            await show(callback,'What happened? Open your eligible claim to submit evidence, or contact support.',reply_markup=kb([[Button(text='Warranty / refund' if claim else 'Contact support',callback_data=f'warranty_order_{int(value)}' if claim else 'support_ticket')],[Button(text='Back',callback_data=f'order_detail_{int(value)}')]]))
        elif action=='cancel':
            existing=await asyncio.to_thread(tools.cancellation_context,user_id,int(value))
            if existing:
                await show(callback,f"Cancellation: {existing['status']}\n{existing['resolution'] or 'Awaiting admin review.'}",reply_markup=home())
            else:
                await state.set_state(CustomerInput.cancel_reason)
                await state.update_data(customer_order=int(value))
                await show(callback,'Why do you want to cancel? Approval is possible only before delivery. Send 5–1,000 characters.',state=state,reply_markup=kb([[Button(text='Back',callback_data=f'order_detail_{int(value)}')]]))
        elif action in ('cancelreview','cancelaccept','canceldecline'):
            if user_id not in ADMIN_IDS: raise ValueError('Access denied.')
            if action!='cancelreview':
                await asyncio.to_thread(tools.review_cancellation,user_id,int(value),action=='cancelaccept')
            row=await asyncio.to_thread(tools.cancellation_context,user_id,int(value),True)
            if not row: raise ValueError('Request not found.')
            buttons=[[Button(text='Approve & refund',callback_data=f'ct_cancelaccept_{int(value)}',style='danger'),Button(text='Decline',callback_data=f'ct_canceldecline_{int(value)}')]] if row['status']=='submitted' else []
            await show(callback,f"Order #{int(value)} · Cancellation\n{row['status']}\n\n{row['reason']}",reply_markup=kb(buttons+[[Button(text='Order',callback_data=f'order_{int(value)}')]]),parse_mode=None)
        elif action=='thread':
            await state.clear()
            await thread_screen(callback,int(value))
        elif action in ('ask','reply'):
            if action=='ask' and user_id not in ADMIN_IDS: raise ValueError('Access denied.')
            await asyncio.to_thread(tools.claim_conversation,user_id,int(value),action=='ask')
            await state.set_state(CustomerInput.admin_reply if action=='ask' else CustomerInput.claim_reply)
            await state.update_data(customer_order=int(value))
            await show(callback,'Send your message (5–1,500 characters). You can attach a screenshot with a caption up to 2 MB.',state=state,reply_markup=kb([[Button(text='Back',callback_data=f'ct_thread_{int(value)}')]]))
        elif action=='proof':
            data,order_id=await asyncio.to_thread(tools.claim_reply_image,user_id,int(value),user_id in ADMIN_IDS)
            if not data: raise ValueError('Image unavailable.')
            photo=await callback.message.answer_photo(BufferedInputFile(data,filename='claim-proof.jpg'),caption=f'Order #{order_id} · Claim evidence',reply_markup=kb([[Button(text='Back',callback_data=f'ct_thread_{order_id}')]]))
            await callback.message.delete()
            track_card(user_id,photo.chat.id,photo.message_id)
        elif action=='hold':
            if user_id not in ADMIN_IDS: raise ValueError('Access denied.')
            await state.set_state(CustomerInput.hold_resolution)
            await state.update_data(customer_cart=value)
            await show(callback,'Investigate the provider result first. Do not declare no charge after an ambiguous timeout.\n\nSend ORDER <existing order ID> <investigation note>, or NO_CHARGE <provider confirmation note>. Notes must contain 20–1,000 characters.',state=state,reply_markup=home())
        elif action=='term':
            if user_id not in ADMIN_IDS: raise ValueError('Access denied.')
            await state.set_state(CustomerInput.term_days)
            await state.update_data(customer_product=int(value))
            await show(callback,'Send actual subscription days (1–3,650), or 0 to disable reminders for new orders. This is separate from warranty/refund coverage.',state=state,reply_markup=kb([[Button(text='Back',callback_data=f'manage_{int(value)}')]]))
    except (ValueError,TypeError) as error:
        await show(callback,escape(str(error)),parse_mode='HTML',reply_markup=home())


@router.message(CustomerInput.quantity)
@router.message(CustomerInput.cancel_reason)
@router.message(CustomerInput.claim_reply)
@router.message(CustomerInput.admin_reply)
@router.message(CustomerInput.term_days)
@router.message(CustomerInput.hold_resolution)
async def customer_input(message:Message,state:FSMContext):
    data=await state.get_data()
    mode=await state.get_state()
    try:
        if (message.text or '').strip().lower()=='/cancel':
            await state.clear()
            await update_card(message,text='Cancelled.',reply_markup=home())
            return
        if mode==CustomerInput.quantity.state:
            await asyncio.to_thread(cart.change,message.from_user.id,data['customer_product'],int(message.text or ''))
            await state.clear()
            await cart_screen(message)
        elif mode==CustomerInput.cancel_reason.state:
            status=await asyncio.to_thread(tools.request_cancellation,message.from_user.id,data['customer_order'],message.text or '')
            await state.clear()
            await update_card(message,text=f'Cancellation {status}. Check your order for the outcome.',reply_markup=home())
        elif mode==CustomerInput.hold_resolution.state:
            if message.from_user.id not in ADMIN_IDS: raise ValueError('Access denied.')
            command=(message.text or '').split(maxsplit=2)
            if len(command)>=3 and command[0].upper()=='ORDER':
                order_id,note=int(command[1]),command[2]
            elif len(command)>=2 and command[0].upper()=='NO_CHARGE':
                order_id,note=None,(message.text or '').split(maxsplit=1)[1]
            else: raise ValueError('Use ORDER <id> <note> or NO_CHARGE <provider confirmation note>.')
            await asyncio.to_thread(cart.resolve_hold,message.from_user.id,data['customer_cart'],order_id,note)
            await state.clear()
            await update_card(message,text='✅ Cart hold resolved and recorded in the admin audit log.',reply_markup=home())
        elif mode==CustomerInput.term_days.state:
            if message.from_user.id not in ADMIN_IDS: raise ValueError('Access denied.')
            days=int(message.text or '')
            if not 0<=days<=3650: raise ValueError('Use 0–3,650 days.')
            def save():
                from database import transaction
                from models.product import Product
                with transaction() as db:
                    product=db.query(Product).filter_by(id=data['customer_product']).with_for_update().one()
                    product.term_days=days
            await asyncio.to_thread(save)
            await state.clear()
            await update_card(message,text='✅ Subscription reminder term saved for new orders.',reply_markup=kb([[Button(text='Product',callback_data=f"manage_{data['customer_product']}")]]))
        else:
            image=None
            if message.photo:
                if (message.photo[-1].file_size or 0)>2_000_000: raise ValueError('Use a screenshot below 2 MB.')
                stream=await asyncio.wait_for(message.bot.download(message.photo[-1].file_id,destination=BytesIO()),20)
                image=stream.getvalue()
            await asyncio.to_thread(tools.claim_reply,message.from_user.id,data['customer_order'],message.text or message.caption or '',mode==CustomerInput.admin_reply.state,image)
            await state.clear()
            await thread_screen(message,data['customer_order'])
    except (ValueError,TypeError) as error:
        await update_card(message,state=state,text=escape(str(error))+'\nTry again or /cancel.',parse_mode='HTML',reply_markup=home())
