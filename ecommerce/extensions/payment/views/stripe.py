

import logging

from django.contrib.auth.decorators import login_required
from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.decorators import method_decorator
from django.views.generic import View
from oscar.apps.partner import strategy
from oscar.apps.payment.exceptions import PaymentError
from oscar.core.loading import get_class, get_model

from ecommerce.extensions.basket.utils import basket_add_organization_attribute, basket_add_payment_intent_id_attribute
from ecommerce.extensions.checkout.mixins import EdxOrderPlacementMixin
from ecommerce.extensions.checkout.utils import get_receipt_page_url
from ecommerce.extensions.payment.core.sdn import checkSDN
from ecommerce.extensions.payment.forms import StripeSubmitForm
from ecommerce.extensions.payment.processors.stripe import Stripe
from ecommerce.extensions.payment.views import BasePaymentSubmitView

logger = logging.getLogger(__name__)

Applicator = get_class('offer.applicator', 'Applicator')
BasketAttribute = get_model('basket', 'BasketAttribute')
BillingAddress = get_model('order', 'BillingAddress')
Country = get_model('address', 'Country')
NoShippingRequired = get_class('shipping.methods', 'NoShippingRequired')
OrderTotalCalculator = get_class('checkout.calculators', 'OrderTotalCalculator')
PaymentProcessorResponse = get_model('payment', 'PaymentProcessorResponse')


class StripeSubmitView(EdxOrderPlacementMixin, BasePaymentSubmitView):
    """ Stripe payment handler.

    The payment form should POST here. This view will handle creating the charge at Stripe, creating an order,
    and redirecting the user to the receipt page.
    """
    form_class = StripeSubmitForm

    @property
    def payment_processor(self):
        return Stripe(self.request.site)

    def form_valid(self, form):
        form_data = form.cleaned_data
        basket = form_data['basket']
        payment_intent_id = form_data['payment_intent_id']
        order_number = basket.order_number

        basket_add_organization_attribute(basket, self.request.POST)
        basket_add_payment_intent_id_attribute(basket, self.request.POST)

        try:
            self.handle_payment(payment_intent_id, basket)
        except Exception:  # pylint: disable=broad-except
            logger.exception('An error occurred while processing the Stripe payment for basket [%d].', basket.id)
            return JsonResponse({}, status=400)

        try:
            order = self.create_order(self.request, basket)
        except Exception:  # pylint: disable=broad-except
            logger.exception('An error occurred while processing the Stripe payment for basket [%d].', basket.id)
            return JsonResponse({}, status=400)

        self.handle_post_order(order)

        receipt_url = get_receipt_page_url(
            self.request,
            site_configuration=self.request.site.siteconfiguration,
            order_number=order_number,
            disable_back_button=True
        )
        return JsonResponse({'url': receipt_url}, status=201)


class StripeCheckoutView(EdxOrderPlacementMixin, View):
    http_method_names = ['post']

    @property
    def payment_processor(self):
        return Stripe(self.request.site)

    def check_sdn(self, request, data):
        """
        Check that the supplied request and form data passes SDN checks.

        Returns:
            hit_count (int) if the SDN check fails, or None if it succeeds.
        """
        hit_count = checkSDN(
            request,
            data['name'],
            data['city'],
            data['country'])

        if hit_count > 0:
            logger.info(
                'SDNCheck function called for basket [%d]. It received %d hit(s).',
                request.basket.id,
                hit_count,
            )
            return hit_count

        logger.info(
            'SDNCheck function called for basket [%d]. It did not receive a hit.',
            request.basket.id,
        )
        return None

    def _get_basket(self, payment_intent_id):
        """
        Retrieve a basket using a payment intent ID.

        Arguments:
            payment_intent_id: payment_intent_id received from Stripe.

        Returns:
            It will return related basket or log exception and return None if
            duplicate payment_intent_id* received or any other exception occurred.
        """
        try:
            basket_attribute = BasketAttribute.objects.get(value_text=payment_intent_id)
            basket = basket_attribute.basket
            basket.strategy = strategy.Default()

            Applicator().apply(basket, basket.owner, self.request)

            basket_add_organization_attribute(basket, self.request.GET)
        except MultipleObjectsReturned:
            logger.warning(u"Duplicate payment_intent_id [%s] received from Stripe.", payment_intent_id)
            return None
        except ObjectDoesNotExist:
            logger.warning(u"Could not find payment_intent_id [%s] among baskets.", payment_intent_id)
            return None
        except Exception:  # pylint: disable=broad-except
            logger.exception(u"Unexpected error during basket retrieval while executing Stripe payment.")
            return None
        return basket

    @method_decorator(login_required)
    def post(self, request):
        """Handle an incoming user returned to us by Stripe after approving payment."""
        # TBD: we're gonna want to check the $$ price of paymentIntentId
        # to see if it suceeded or failed
        # ... and then potentially compare it against what our basket has?
        stripe_response = request.POST.dict()
        payment_intent_id = stripe_response.get('payment_intent_id')

        logger.info(
            '%s called for payment intent id [%s].',
            self.__class__.__name__,
            request.POST.get('payment_intent_id')
        )

        basket = self._get_basket(payment_intent_id)

        if not basket:
            return redirect(self.payment_processor.error_url)

        receipt_url = get_receipt_page_url(
            self.request,
            order_number=basket.order_number,
            site_configuration=basket.site.siteconfiguration,
            disable_back_button=True
        )

        # SDN Check here!
        billing_address_obj = self.payment_processor.get_address_from_token(
            payment_intent_id
        )
        sdn_check_data = {
            # Stripe has 1 name field so we use first_name on billing_address_obj
            'name': billing_address_obj.first_name,
            'city': billing_address_obj.city,
            'country': billing_address_obj.country,
        }
        sdn_check_failure = self.check_sdn(self.request, sdn_check_data)
        if sdn_check_failure is not None:
            return redirect(self.payment_processor.error_url)

        try:
            with transaction.atomic():
                try:
                    self.handle_payment(stripe_response, basket)
                except PaymentError:
                    return redirect(self.payment_processor.error_url)
        except:  # pylint: disable=bare-except
            logger.exception('Attempts to handle payment for basket [%d] failed.', basket.id)
            return redirect(receipt_url)

        try:
            billing_address = self.create_billing_address(
                user=self.request.user,
                billing_address=billing_address_obj
            )
        except Exception as err:  # pylint: disable=broad-except
            logger.exception('Error creating billing address for basket [%d]: %s', basket.id, err)
            billing_address = None

        try:
            order = self.create_order(request, basket, billing_address)
        except Exception:  # pylint: disable=broad-except
            return redirect(receipt_url)

        try:
            self.handle_post_order(order)
        except Exception:  # pylint: disable=broad-except
            self.log_order_placement_exception(basket.order_number, basket.id)

        return redirect(receipt_url)
